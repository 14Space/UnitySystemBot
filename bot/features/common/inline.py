import json

from aiogram import Router, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineQuery, InlineQueryResultCachedVideo, InlineQueryResultCachedAudio,
    InlineQueryResultCachedPhoto, InlineQueryResultCachedMpeg4Gif,
    InlineQueryResultArticle, InputTextMessageContent,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from bot.database import SessionLocal
from bot.database.repository import get_any_cached_file
from bot.features.download.link import stash_inline_link
from bot.utils.platform_detector import detect_platform, Platform, normalize_cache_url
from bot.utils.i18n import t, lang_of

router = Router()

# Имя бота кэшируем ПО КАЖДОМУ боту: в мульти-бот режиме код общий, и один общий кэш
# указывал бы кнопку «Скачать в боте» на чужого бота (открыл бы не того).
_bot_usernames: dict[int, str] = {}


async def _username(bot: Bot) -> str:
    if bot.id not in _bot_usernames:
        me = await bot.get_me()
        _bot_usernames[bot.id] = me.username
    return _bot_usernames[bot.id]


def _result_from_cache(cached: dict, lang: str):
    """Строит один инлайн-результат из кэша, или None, если инлайном отдать нельзя
    (альбом из нескольких медиа — Telegram inline шлёт только одно сообщение)."""
    fid, quality = cached["file_id"], cached["quality"]

    # X/Twitter: в кэше JSON с медиа-токенами. Одиночный пост можно отдать инлайном,
    # альбом (2+ медиа) — нет.
    if quality == "x":
        try:
            items = json.loads(fid).get("items", [])
        except Exception:
            return None
        if len(items) != 1:
            return None
        it = items[0]
        k, mid = it.get("k"), it.get("id")
        if not mid:
            return None
        if k == "P":
            return InlineQueryResultCachedPhoto(id="1", photo_file_id=mid)
        if k == "G":
            return InlineQueryResultCachedMpeg4Gif(id="1", mpeg4_file_id=mid)
        return InlineQueryResultCachedVideo(
            id="1", video_file_id=mid, title=t("inline_cached_video_title", lang)
        )

    if quality == "audio":
        return InlineQueryResultCachedAudio(id="1", audio_file_id=fid)
    if fid.startswith("P:"):
        return InlineQueryResultCachedPhoto(id="1", photo_file_id=fid[2:])
    video_id = fid[2:] if fid.startswith("V:") else fid
    return InlineQueryResultCachedVideo(
        id="1", video_file_id=video_id, title=t("inline_cached_video_title", lang)
    )


@router.inline_query()
async def inline_handler(query: InlineQuery, bot: Bot):
    text = query.query.strip()
    lang = lang_of(query.from_user)

    # Нет ссылки или платформа не поддержана — ничего не показываем
    if not text.startswith("http") or detect_platform(text) == Platform.UNKNOWN:
        await query.answer([], cache_time=5, is_personal=True)
        return

    # Уже качали (есть в кэше) — отдаём файл мгновенно прямо в чат.
    # Ключ каноничный (у X одну и ту же ссылку шлют с разными хвостами/зеркалами).
    async with SessionLocal() as session:
        cached = await get_any_cached_file(session, normalize_cache_url(text))

    result = _result_from_cache(cached, lang) if cached else None
    if result is not None:
        try:
            await query.answer([result], cache_time=0, is_personal=True)
            return
        except TelegramBadRequest:
            # file_id привязан к боту, который его сохранил: если файл закэшировал
            # другой бот, этот id для нас невалиден (DOCUMENT_INVALID). Тогда просто
            # отдаём обычную карточку-переход, как для незнакомой ссылки.
            pass

    # Ссылки нет в кэше (или чужой file_id) — за пару секунд не скачать. Даём кнопку-переход в бота.
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
