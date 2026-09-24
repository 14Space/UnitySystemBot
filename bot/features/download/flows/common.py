"""
Общие детали обработчиков ссылок: защита от повторного нажатия, ответ об ошибке,
отправка файлов и альбомов, имя файла для человека, параметры видео для Telegram.

Вынесено из link.py, когда он разросся до 1600 строк: этим пользуются все площадки,
и держать общее рядом с частным значило искать его по всему файлу.
"""

import asyncio
import os
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile, InputMediaPhoto, InputMediaVideo,
)
from bot.config import GROUP_TYPES, SHORTS_CAP_HEIGHT
from bot.utils import limits, tg_files, media_names
from bot.utils.tg_messages import safe_delete
from bot.utils.i18n import t
from bot.database import SessionLocal
from bot.database.repository import get_compress_shorts
from bot.features.download import job
from bot.features.download.job import Cache
from bot.features.download.downloaders.instagram import is_image
from bot.features.download.downloaders.video_meta import probe_video, make_video_thumbnail


# Нажатия, которые СОЗДАЮТ контент: слайдшоу TikTok, трек из коллекции. Одно нажатие —
# один результат. Защиты тут не было вовсе: кнопки убираются только ПОСЛЕ отправки, а
# за время скачивания успевают запуститься несколько обработчиков — человек нажал
# «Фото» пять раз и получил пять одинаковых наборов. У тяжёлых загрузок своя защита
# (job.exclusive, «дождись текущей»), а у лёгких не было никакой.
_IN_PROGRESS: set[tuple] = set()


def _claim(*key) -> bool:
    """Занимает нажатие. False — точно такое же уже выполняется прямо сейчас."""
    if key in _IN_PROGRESS:
        return False
    _IN_PROGRESS.add(key)
    return True


def _unclaim(*key) -> None:
    _IN_PROGRESS.discard(key)


def _reply_error(message: Message, lang: str, fallback_key: str | None = None):
    """Сбой загрузки – понятным текстом в ответ на сообщение человека. fallback_key –
    платформенная подсказка (например, про приватность), если причина не распознана."""
    async def on_error(e: Exception):
        msg = limits.friendly_error(e, lang)
        if fallback_key and msg == t("generic_dl_failed", lang):
            msg = t(fallback_key, lang)
        await message.reply(msg)
    return on_error


async def _quiet(e: Exception):
    """Сбой, о котором человеку не говорим: это был бонус, а не то, что он просил."""


async def _shorts_cap(chat) -> int | None:
    """Потолок качества для коротких видео («Сжатие шортс») или None без ограничения.
    Величину потолка задаёт SHORTS_CAP_HEIGHT (короткая сторона кадра). Дефолт тумблера
    зависит от типа чата: в группах ВКЛ (упор на скорость), в личке ВЫКЛ (упор на
    качество). Пользователь может переключить в /setconfig."""
    in_group = chat.type in GROUP_TYPES
    async with SessionLocal() as session:
        on = await get_compress_shorts(session, chat.id, default=in_group)
    return SHORTS_CAP_HEIGHT if on else None


def _shorts_key(base: str, cap: int | None) -> str:
    """Ключ кэша с учётом сжатия: сжатая и полная версии не должны подменять друг друга
    (иначе в личку прилетела бы сжатая версия, закэшированная группой, и наоборот)."""
    return f"{base}_c" if cap else base


def _pick_thumbnail(info: dict) -> str | None:
    """Ссылка на превью для экрана выбора качества.

    yt-dlp кладёт в «thumbnail» свой лучший вариант, но у YouTube это webp, который
    Telegram забирает через раз. Поэтому сначала ищем jpeg среди всех предложенных
    (они отсортированы от худшего к лучшему — идём с конца), и только если не нашли,
    берём то, что дал yt-dlp.
    """
    for thumb in reversed(info.get("thumbnails") or []):
        u = thumb.get("url") or ""
        if ".jpg" in u or ".jpeg" in u:
            return u
    return info.get("thumbnail")


async def _heavy_from_cache(callback: CallbackQuery, cache: Cache, send) -> bool | None:
    """Готовое тяжёлое видео из кэша: убираем меню и отдаём.

    True – отдали. None – кэша нет, на нажатие ещё не отвечали. False – кэш был, но
    расписка оказалась мёртвой: на нажатие уже ответили и меню убрали, качаем заново.
    """
    if await cache.get() is None:
        return None
    await callback.answer()
    await safe_delete(callback.message)
    return await job.try_cached(cache, send)


async def _alert(callback: CallbackQuery, answered: bool, chat_id: int, text: str):
    """Отказ по нажатию: всплывающим окном, а если на нажатие уже ответили (Telegram
    второй ответ не примет) – сообщением в чат."""
    if answered:
        await callback.bot.send_message(chat_id, text)
    else:
        await callback.answer(text, show_alert=True)


async def _send_cached_post(message: Message, cached: str):
    """Переотправляет ранее сохранённый пост из кэша (file_id'ы через перевод строки,
    каждый с префиксом типа: «P:» фото, «V:» видео)."""
    tokens = cached.split("\n")
    if len(tokens) == 1:
        tok = tokens[0]
        if tok.startswith("P:"):
            await message.reply_photo(tok[2:])
        else:
            await message.reply_video(tok[2:], supports_streaming=True)
        return
    for chunk in _chunked(tokens, 10):
        media = []
        for tok in chunk:
            if tok.startswith("P:"):
                media.append(InputMediaPhoto(media=tok[2:]))
            else:
                media.append(InputMediaVideo(media=tok[2:], supports_streaming=True))
        await message.reply_media_group(media)


async def _send_media_files(message: Message, files: list[str], lang: str) -> list[str]:
    """Отправляет файлы (фото/видео) одиночно или альбомом. Возвращает токены file_id
    ('P:' фото, 'V:' видео) для сохранения в кэш."""
    tokens: list[str] = []
    # Имена по техническому номеру поста: подписи у этих площадок не гарантированы.
    # Когда файлов несколько, к номеру добавляем порядковый — иначе все файлы поста
    # получили бы ОДНО имя, и раскладывал бы их по порядку не бот, а Telegram.
    multi = len(files) > 1

    async def _nm(path: str, i: int):
        return await _nice_name(path, quality="" if is_image(path) else None,
                                use_title=False, index=(i + 1) if multi else None)

    if len(files) == 1:
        f = files[0]
        if is_image(f):
            sent = await message.reply_photo(await tg_files.input_file_async(f, await _nm(f, 0)))
            if sent.photo:
                tokens.append("P:" + sent.photo[-1].file_id)
        else:
            sent = await message.reply_video(await tg_files.input_file_async(f, await _nm(f, 0)),
                                             **await _video_kwargs(f))
            if sent.video:
                tokens.append("V:" + sent.video.file_id)
    else:
        idx = 0
        for chunk in _chunked(files, 10):
            media = []
            for f in chunk:
                name = await _nm(f, idx)
                idx += 1
                if is_image(f):
                    media.append(InputMediaPhoto(media=await tg_files.input_file_async(f, name)))
                else:
                    media.append(InputMediaVideo(media=await tg_files.input_file_async(f, name),
                                                 **await _video_kwargs(f)))
            sent_msgs = await message.reply_media_group(media)
            for m in sent_msgs:
                if m.photo:
                    tokens.append("P:" + m.photo[-1].file_id)
                elif m.video:
                    tokens.append("V:" + m.video.file_id)
    return tokens


def _chunked(items: list, size: int):
    """Разбивает список на куски по size элементов."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def _nice_name(path: str, quality: str | None = None, *,
                     use_title: bool = True, index: int | None = None) -> str | None:
    """Имя, под которым файл придёт человеку.

    Две схемы, и выбор между ними — про НАДЁЖНОСТЬ, а не про красоту:

    • use_title=True — берём настоящее название площадки: «Название [1080p] @Бот.mp4».
      Так делаем там, где название есть гарантированно: YouTube (включая Shorts),
      PornHub, HDRezka.
    • use_title=False — берём технический номер ролика: «7106594312292453675 [720p]
      @Бот.mp4». Так делаем там, где вместо названия приходит подпись автора: её может
      не быть вовсе, она бывает из одних хештегов или из одних эмодзи. Номер есть
      всегда и не преподносит сюрпризов.

    index — порядковый номер файла в посте из нескольких (карусель, слайдшоу, твит с
    несколькими фото). Без него все файлы поста получили бы ОДНО имя, и раскладывал бы
    их по порядку уже не бот, а Telegram — как придётся.

    quality=None — определить по самому файлу; quality="" — не писать вовсе (у фото и
    музыки качества нет).
    """
    title, ident = media_names.peek(path)
    base = (title if use_title else None) or ident
    if not base:
        return None
    if index is not None:
        base = f"{base} ({index})"
    ext = os.path.splitext(path)[1] or ".mp4"
    if quality is None and ext.lower() in (".mp4", ".mov", ".mkv", ".webm"):
        try:
            meta = await asyncio.to_thread(probe_video, path)
            # КОРОТКАЯ сторона кадра: у вертикального ролика 720x1280 качество — 720p.
            # На этом в проекте уже обжигались, приняв за качество высоту.
            side = min(meta.get("width") or 0, meta.get("height") or 0)
            quality = f"{side}p" if side else None
        except Exception:
            quality = None
    return tg_files.display_name(base, quality or "", ext)


async def _video_kwargs(file_path: str, duration: int = 0) -> dict:
    """
    Собирает width/height/duration/thumbnail для send_video/reply_video.

    Без этих параметров Telegram (особенно на iOS) не знает соотношение сторон:
    рисует «сплюснутое» превью, на котором плеер виснет до полной докачки. Поэтому
    зондируем файл ffprobe'ом и прикладываем постер-кадр — видео сразу корректно
    показывается и стримится на лету.
    """
    # Оба вызова читают один и тот же файл и друг от друга не зависят — запускаем разом.
    meta, thumb_bytes = await asyncio.gather(
        asyncio.to_thread(probe_video, file_path),
        asyncio.to_thread(make_video_thumbnail, file_path),
    )
    thumbnail = BufferedInputFile(thumb_bytes, filename="thumb.jpg") if thumb_bytes else None
    return dict(
        duration=duration or meta["duration"],
        width=meta["width"] or None,
        height=meta["height"] or None,
        thumbnail=thumbnail,
        supports_streaming=True,  # видео можно смотреть на лету, не дожидаясь полной загрузки
    )
