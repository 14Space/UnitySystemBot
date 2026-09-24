"""
X (Twitter): один пост – фото, видео, gif и текст; чисто текстовый пост и цитата –
карточкой-картинкой.
"""

import asyncio
import html
import json
import logging
from aiogram.types import Message, InputMediaPhoto, InputMediaVideo
from bot.utils import limits, tg_files
from bot.utils.i18n import t
from bot.features.download import job
from bot.features.download.job import Cache
from bot.features.download.downloaders import twitter
from bot.features.download.renderer.tweet_card import render_tweet_card
from bot.features.download.flows.common import _nice_name, _reply_error, _video_kwargs

logger = logging.getLogger(__name__)


# Telegram: подпись к медиа — максимум 1024 символа (у обычного текста 4096).
TWEET_CAPTION_MAX = 1024


async def _handle_twitter(message: Message, url: str, lang: str):
    """X (Twitter): один пост. Три случая в одном потоке (+ кэш по ссылке):
      • есть медиа        → фото/видео/gif + текст подписью;
      • чисто текст       → карточка-скриншот твита;
      • цитата-твит       → карточка + медиа цитаты + текст цитаты «цитатой» снизу.
    """
    # Кэш: этот твит уже отправляли — мгновенно переотправляем по file_id. Значение —
    # JSON с токенами и подписью.
    cache = Cache(url, "x")
    if await job.try_cached(cache, lambda raw: _send_cached_tweet(message, json.loads(raw))):
        return

    try:
        tweet = await asyncio.to_thread(twitter.get_tweet, url)
    except Exception as e:
        logger.exception("Twitter fetch failed")
        await message.reply(limits.friendly_error(e, lang))
        return

    async def work(paths):
        items, caption, parse_mode = await _build_twitter_plan(tweet)
        paths.keep([it["path"] for it in items])
        if not items:
            # карточка не нарисовалась — отдаём хотя бы текст
            await message.reply(caption or t("no_media", lang))
            return None
        tokens = await _send_twitter(message, items, caption, parse_mode)
        if not tokens:
            return None
        return json.dumps({"items": tokens, "caption": caption, "pm": parse_mode})

    await job.produce(cache=cache, work=work, on_error=_reply_error(message, lang),
                      tell=message, lang=lang, label=f"X {url}")


async def _build_twitter_plan(tweet: dict) -> tuple[list[dict], str | None, str | None]:
    """Готовит к отправке: (список медиа [{'kind','path'}], подпись, parse_mode).
    Скачивает файлы / рисует карточку. Пустой список = отдать текстом (карточка не вышла)."""
    # 1) Пост со своим медиа
    if tweet["media"]:
        files = await asyncio.to_thread(twitter.download_media, tweet["media"], tweet["id"])
        caption = tweet["text"][:TWEET_CAPTION_MAX] if tweet["text"] else None
        return files, caption, None

    # 2) Чисто текстовый твит — карточка-скриншот
    if tweet["quote"] is None:
        try:
            card = await asyncio.to_thread(render_tweet_card, tweet)
            return [{"kind": "photo", "path": card}], None, None
        except Exception:
            logger.exception("Tweet card render failed")
            return [], tweet["text"], None  # пусто → вызывающий отправит текст

    # 3) Цитата-твит: карточка всего поста + медиа цитаты + текст цитаты «цитатой»
    quote = tweet["quote"]
    card = await asyncio.to_thread(render_tweet_card, tweet)
    items = [{"kind": "photo", "path": card}]
    if quote["media"]:
        try:
            qfiles = await asyncio.to_thread(
                twitter.download_media, quote["media"], tweet["id"] + "_q")
        except Exception:
            # Медиа цитаты не скачалось — карточка уже нарисована и лежит на диске.
            # Раньше исключение уходило выше, и файл оставался до перезапуска бота.
            logger.exception("Медиа цитаты не скачалось — отдаю только карточку")
            qfiles = []
        items += qfiles
    caption, parse_mode = None, None
    if quote["text"]:
        safe = html.escape(quote["text"][:TWEET_CAPTION_MAX - 30])
        caption, parse_mode = f"<blockquote>{safe}</blockquote>", "HTML"
    return items, caption, parse_mode


async def _send_twitter(message: Message, items: list[dict], caption: str | None,
                        parse_mode: str | None) -> list[dict]:
    """Отправляет медиа твита (одно или альбомом) и возвращает токены file_id для кэша:
    [{'k': 'P'|'V'|'G', 'id': ...}]. Подпись крепится к первому элементу."""
    tokens: list[dict] = []
    multi = len(items) > 1

    async def _nm(path: str, kind: str, i: int):
        return await _nice_name(path, quality="" if kind in ("photo", "gif") else None,
                                use_title=False, index=(i + 1) if multi else None)

    if len(items) == 1:
        it = items[0]
        f = await tg_files.input_file_async(it["path"], await _nm(it["path"], it["kind"], 0))
        if it["kind"] == "photo":
            sent = await message.reply_photo(f, caption=caption, parse_mode=parse_mode)
            if sent.photo:
                tokens.append({"k": "P", "id": sent.photo[-1].file_id})
        elif it["kind"] == "gif":
            sent = await message.reply_animation(f, caption=caption, parse_mode=parse_mode)
            if sent.animation:
                tokens.append({"k": "G", "id": sent.animation.file_id})
        else:
            sent = await message.reply_video(f, caption=caption, parse_mode=parse_mode,
                                             **await _video_kwargs(it["path"]))
            if sent.video:
                tokens.append({"k": "V", "id": sent.video.file_id})
        return tokens

    media = []
    for i, it in enumerate(items):
        cap = caption if i == 0 else None
        pm = parse_mode if i == 0 else None
        f = await tg_files.input_file_async(it["path"], await _nm(it["path"], it["kind"], i))
        if it["kind"] == "photo":
            media.append(InputMediaPhoto(media=f, caption=cap, parse_mode=pm))
        else:
            # gif внутри альбома Telegram показывает как видео — это нормально
            media.append(InputMediaVideo(media=f, caption=cap, parse_mode=pm,
                                         **await _video_kwargs(it["path"])))
    sent_msgs = await message.reply_media_group(media)
    for m in sent_msgs:
        if m.photo:
            tokens.append({"k": "P", "id": m.photo[-1].file_id})
        elif m.animation:
            tokens.append({"k": "G", "id": m.animation.file_id})
        elif m.video:
            tokens.append({"k": "V", "id": m.video.file_id})
    return tokens


async def _send_cached_tweet(message: Message, data: dict):
    """Переотправляет твит из кэша по сохранённым file_id (без скачивания/рендера)."""
    items = data.get("items") or []
    caption = data.get("caption")
    pm = data.get("pm")
    if not items:
        return
    if len(items) == 1:
        it = items[0]
        if it["k"] == "P":
            await message.reply_photo(it["id"], caption=caption, parse_mode=pm)
        elif it["k"] == "G":
            await message.reply_animation(it["id"], caption=caption, parse_mode=pm)
        else:
            await message.reply_video(it["id"], caption=caption, parse_mode=pm, supports_streaming=True)
        return
    media = []
    for i, it in enumerate(items):
        cap = caption if i == 0 else None
        p = pm if i == 0 else None
        if it["k"] == "P":
            media.append(InputMediaPhoto(media=it["id"], caption=cap, parse_mode=p))
        else:
            media.append(InputMediaVideo(media=it["id"], caption=cap, parse_mode=p, supports_streaming=True))
    await message.reply_media_group(media)
