"""
Команда /ai — ИИ-ассистент. Ответь этой командой на любое сообщение (тогда оно —
контекст), либо напиши вопрос прямо: «/ai сколько лететь до Марса». Уточнения —
ответом на ответ бота (тред как у Грока в твиттере).

Работает, если функция «ai» не выключена в чате через /setconfig. Держится в
бесплатном тире: свои суточные лимиты + мягкая обработка «лимит провайдера исчерпан».
"""
import asyncio
import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.config import AI_DAILY_LIMIT, AI_USER_DAILY_LIMIT
from bot.database import SessionLocal
from bot.database.repository import (
    get_disabled_features, save_ai_thread, load_ai_thread, ai_usage_today, add_ai_usage)
from bot.features.ai import client
from bot.utils.i18n import t, lang_of

router = Router()
logger = logging.getLogger(__name__)

GROUP_TYPES = ("group", "supergroup")
MAX_LEN = 4000            # запас под лимит Telegram 4096
_MAX_TURNS = 12           # сколько последних реплик держим в контексте

# Ветки разговора и суточные счётчики лежат в БАЗЕ, а не в памяти. Причина одна и та
# же для обоих: сообщения в чате живут дольше, чем процесс бота.
#   • ветка — потому что ответить на вчерашнюю реплику бота человек может в любой
#     момент, и после перезапуска разговор начинался с чистого листа;
#   • счётчик — потому что лимит на сутки держит нас в бесплатном тире провайдера, а
#     перезапуск обнулял его: лимит обходился ожиданием ближайшего деплоя.


async def _within_limits(user_id: int) -> bool:
    async with SessionLocal() as session:
        mine, total = await ai_usage_today(session, user_id)
    return total < AI_DAILY_LIMIT and mine < AI_USER_DAILY_LIMIT


async def _count(user_id: int):
    async with SessionLocal() as session:
        await add_ai_usage(session, user_id)


async def _remember(msg_id: int, chat_id: int, history: list[dict]):
    async with SessionLocal() as session:
        await save_ai_thread(session, msg_id, chat_id, history[-_MAX_TURNS:])


async def _ai_disabled(chat) -> bool:
    """Выключен ли ИИ в этом чате через /setconfig (в группах и в личке хаба)."""
    async with SessionLocal() as session:
        disabled = await get_disabled_features(session, chat.id)
    return "ai" in disabled


@router.message(Command("ai"))
async def cmd_ai(message: Message, command: CommandObject):
    lang = lang_of(message.from_user)
    if await _ai_disabled(message.chat):
        return

    question = (command.args or "").strip()
    replied = message.reply_to_message

    # Собираем контекст: если ответили на прошлый ответ бота — продолжаем тот тред;
    # если на чужое сообщение — берём его текстом как контекст.
    history: list[dict] = []
    if replied:
        async with SessionLocal() as session:
            thread = await load_ai_thread(session, replied.message_id)
        if thread:
            history = list(thread)
        else:
            ctx = replied.text or replied.caption
            if ctx:
                history = [{"role": "user", "content": ctx}]
    if question:
        history.append({"role": "user", "content": question})

    if not history:                       # /ai без вопроса и без реплая — подсказка
        await message.reply(t("ai_how", lang))
        return

    if not await _within_limits(message.from_user.id):
        await message.reply(t("ai_limit", lang))
        return

    status = await message.reply(t("ai_thinking", lang))
    text, st = await asyncio.to_thread(client.ask, history, lang)

    if st == "no_provider":
        await _safe_edit(status, t("ai_not_configured", lang))
        return
    if st == "quota":
        await _safe_edit(status, t("ai_quota", lang))
        return
    if st != "ok" or not text:
        await _safe_edit(status, t("ai_error", lang))
        return

    await _count(message.from_user.id)
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN] + "…"
    sent = await _safe_edit(status, text)
    if not sent:
        # Правка не прошла (сообщение удалили, отобрали права) — ответ всё равно должен
        # дойти, иначе человек остаётся с «Думаю…» и без ответа.
        sent = await message.reply(text)
    if sent:
        await _remember(sent.message_id, message.chat.id,
                        history + [{"role": "assistant", "content": text}])


async def _safe_edit(msg: Message, text: str) -> Message | None:
    try:
        return await msg.edit_text(text)
    except Exception:
        return None
