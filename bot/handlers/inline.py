from aiogram import Router, Bot
from aiogram.types import (
    InlineQuery, InlineQueryResultCachedVideo, InlineQueryResultCachedAudio,
    InlineQueryResultCachedPhoto, InlineQueryResultArticle, InputTextMessageContent,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from bot.database import SessionLocal
from bot.database.repository import get_any_cached_file
from bot.handlers.link import stash_inline_link
from bot.utils.platform_detector import detect_platform, Platform
from bot.utils.i18n import t, lang_of

router = Router()

_bot_username = None


async def _username(bot: Bot) -> str:
    global _bot_username
    if _bot_username is None:
        me = await bot.get_me()
        _bot_username = me.username
    return _bot_username


@router.inline_query()
async def inline_handler(query: InlineQuery, bot: Bot):
    text = query.query.strip()
    lang = lang_of(query.from_user)

    # Нет ссылки или платформа не поддержана — ничего не показываем
    if not text.startswith("http") or detect_platform(text) == Platform.UNKNOWN:
        await query.answer([], cache_time=5, is_personal=True)
        return

    # Уже качали (есть в кэше) — отдаём файл мгновенно прямо в чат
    async with SessionLocal() as session:
        cached = await get_any_cached_file(session, text)

    if cached:
        fid, quality = cached["file_id"], cached["quality"]
        if quality == "audio":
            result = InlineQueryResultCachedAudio(id="1", audio_file_id=fid)
        elif fid.startswith("P:"):
            result = InlineQueryResultCachedPhoto(id="1", photo_file_id=fid[2:])
        else:
            video_id = fid[2:] if fid.startswith("V:") else fid
            result = InlineQueryResultCachedVideo(
                id="1", video_file_id=video_id, title=t("inline_cached_video_title", lang)
            )
        await query.answer([result], cache_time=0, is_personal=True)
        return

    # Новой ссылки в кэше нет — за пару секунд её не скачать. Даём кнопку-переход в бота.
    sid = stash_inline_link(text)
    username = await _username(bot)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("btn_download_in_bot", lang), url=f"https://t.me/{username}?start=dl{sid}")
    ]])
    result = InlineQueryResultArticle(
        id="1",
        title=t("inline_article_title", lang),
        description=t("inline_article_desc", lang),
        input_message_content=InputTextMessageContent(message_text=t("inline_message_text", lang, url=text)),
        reply_markup=keyboard,
    )
    await query.answer([result], cache_time=0, is_personal=True)
