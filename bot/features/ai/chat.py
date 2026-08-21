"""
Команда /ai — ИИ-ассистент. Ответь этой командой на любое сообщение (тогда оно —
контекст), либо напиши вопрос прямо: «/ai сколько лететь до Марса». Уточнения —
ответом на ответ бота (тред как у Грока в твиттере).

Работает, если функция «ai» не выключена в чате через /setconfig. Держится в
бесплатном тире: свои суточные лимиты + мягкая обработка «лимит провайдера исчерпан».
"""
import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.config import AI_DAILY_LIMIT, AI_USER_DAILY_LIMIT
from bot.database import SessionLocal
from bot.database.repository import get_disabled_features
from bot.features.ai import client
from bot.utils.i18n import t, lang_of

router = Router()
logger = logging.getLogger(__name__)

GROUP_TYPES = ("group", "supergroup")
MAX_LEN = 4000            # запас под лимит Telegram 4096
_MAX_TURNS = 12           # сколько последних реплик держим в контексте
_MAX_THREADS = 500        # ограничение памяти под треды

# Треды диалога: message_id ответа бота -> история [{role, content}], чтобы уточнения
# «ответом на ответ» продолжали разговор.
_THREADS: dict[int, list[dict]] = {}

# Суточные лимиты (в памяти, сброс в полночь UTC), чтобы не выйти за бесплатный тир.
_usage = {"day": None, "total": 0, "users": {}}


def _reset_if_new_day():
    today = datetime.now(timezone.utc).date()
    if _usage["day"] != today:
        _usage.update(day=today, total=0, users={})


def _within_limits(user_id: int) -> bool:
    _reset_if_new_day()
    return (_usage["total"] < AI_DAILY_LIMIT
            and _usage["users"].get(user_id, 0) < AI_USER_DAILY_LIMIT)


def _count(user_id: int):
    _usage["total"] += 1
    _usage["users"][user_id] = _usage["users"].get(user_id, 0) + 1


def _remember(msg_id: int, history: list[dict]):
    if len(_THREADS) >= _MAX_THREADS:
        _THREADS.pop(next(iter(_THREADS)), None)      # выкидываем самый старый
    _THREADS[msg_id] = history[-_MAX_TURNS:]


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
        thread = _THREADS.get(replied.message_id)
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

    if not _within_limits(message.from_user.id):
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

    _count(message.from_user.id)
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN] + "…"
    sent = await _safe_edit(status, text)
    if sent:
        _remember(sent.message_id, history + [{"role": "assistant", "content": text}])


async def _safe_edit(msg: Message, text: str) -> Message | None:
    try:
        return await msg.edit_text(text)
    except Exception:
        return None
