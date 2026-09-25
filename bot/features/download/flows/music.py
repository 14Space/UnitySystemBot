"""
Музыка: трек Spotify (ищется на YouTube Music, SoundCloud, YouTube), трек SoundCloud и
YT Music, альбомы и плейлисты – списком треков с кнопками.
"""

import asyncio
import logging
from urllib.parse import urlparse, unquote
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from bot.features.download.keyboards.tracklist import build_tracklist_keyboard
from bot.utils import limits, tg_files, chat_action
from bot.utils.tg_messages import safe_edit, safe_delete
from bot.features.common import alerts
from bot.utils.i18n import t, lang_of, t_kind
from bot.database import SessionLocal
from bot.database.repository import is_premium
from bot.features.download import job
from bot.features.download.job import Cache
from bot.features.download.screens import Screens
from bot.features.download.downloaders.ytdlp_wrapper import (
    get_video_info, download_audio, search_audio, search_audio_candidates,
    get_soundcloud_set,
)
from bot.features.download.downloaders.spotify import get_track_info, get_collection_info
from bot.features.download.downloaders.music_search import find_track_sources
from bot.features.download.downloaders.audio_meta import (
    set_metadata, get_soundcloud_cover, make_thumbnail,
)
from bot.features.download.flows.common import _claim, _nice_name, _unclaim

router = Router()
logger = logging.getLogger(__name__)


# Список треков альбома/плейлиста/сета хранится в базе целиком: пересобрать его
# заново – это снова спросить Spotify или SoundCloud, а треки уже известны.
COLLECTIONS = Screens("collection")


def _soundcloud_query(url: str) -> str:
    """Строит поисковый запрос для YouTube из ссылки SoundCloud (на случай DRM).
    soundcloud.com/joaofaria/imagine-dragons-believer -> 'joaofaria imagine dragons believer'"""
    path = urlparse(url).path.strip("/")
    words = unquote(path).replace("/", " ").replace("-", " ").replace("_", " ")
    return " ".join(words.split())


async def _handle_spotify(message: Message, url: str):
    """Читает данные трека Spotify и качает его аудио через поиск на YouTube"""
    try:
        track = await asyncio.to_thread(get_track_info, url)
    except Exception as e:
        logger.exception("Spotify metadata failed")
        alerts.note_failure(e)
        await message.reply(t("track_read_failed", lang_of(message.from_user)))
        return

    # Источник скачивания — поиск на YouTube по «Исполнитель Название».
    # Кэш привязываем к исходной ссылке Spotify, метаданные берём из Spotify.
    source = f"ytsearch1:{track['search_query']}"
    meta = {
        "title": track["title"],
        "performer": track["artist"],
        "duration": track["duration"],
        "cover": track.get("cover"),
    }
    await _handle_audio(message, url, source=source, meta=meta)


async def _handle_spotify_collection(message: Message, url: str, lang: str):
    """Читает альбом/плейлист Spotify и показывает список треков с выбором"""
    try:
        coll = await asyncio.to_thread(get_collection_info, url)
    except Exception as e:
        logger.exception("Spotify collection failed")
        # Spotify с конца 2024 не отдаёт редакционные/алгоритмические плейлисты (id вида 37i9...)
        if "404" in str(e) or "Not Found" in str(e):
            await message.reply(t("spotify_no_playlist", lang))
        else:
            alerts.note_failure(e)    # 404 редакционных плейлистов — норма, остальное — сбой
            await message.reply(t("collection_read_failed", lang))
        return

    if not coll["tracks"]:
        await message.reply(t("empty_album", lang))
        return

    # Приводим к общему формату коллекции: у каждого трека параметры скачивания
    tracks = [{
        "title": t["title"],
        "cache_url": f"https://open.spotify.com/track/{t['id']}",
        "source": f"ytsearch1:{t['query']}",
        "meta": {
            "title": t["title"], "performer": t["artist"],
            "duration": t["duration"], "cover": t.get("cover"),
        },
        "fallback_query": None,
    } for t in coll["tracks"]]

    total_min = coll["total_duration"] // 60
    await _show_collection(
        message, url, coll["kind"], coll["title"], tracks, lang,
        cover=coll.get("cover"), extra=t("minutes_suffix", lang, min=total_min),
    )


async def _handle_soundcloud_set(message: Message, url: str, lang: str):
    """Читает сет (альбом/плейлист) SoundCloud и показывает список треков"""
    try:
        data = await asyncio.to_thread(get_soundcloud_set, url)
    except Exception as e:
        logger.exception("SoundCloud set failed")
        alerts.note_failure(e)
        await message.reply(t("soundcloud_set_failed", lang))
        return

    if not data["tracks"]:
        await message.reply(t("empty_album", lang))
        return

    # У треков SoundCloud прямые ссылки — качаем напрямую, с фолбэком на поиск при DRM
    tracks = [{
        "title": t["title"],
        "cache_url": t["url"],
        "source": t["url"],
        "meta": None,
        "fallback_query": _soundcloud_query(t["url"]),
    } for t in data["tracks"]]

    total_min = sum(tr["duration"] for tr in data["tracks"]) // 60
    extra = t("minutes_suffix", lang, min=total_min) if total_min else ""
    await _show_collection(message, url, "Сет", data["title"], tracks, lang,
                           cover=data.get("cover"), extra=extra)


async def _show_collection(message: Message, url: str, kind: str, title: str, tracks: list,
                           lang: str, cover: str = None, extra: str = ""):
    """Сохраняет коллекцию и показывает список треков с пагинацией"""
    async with SessionLocal() as session:
        premium = await is_premium(session, message.from_user.id)

    # Треки — в базу целиком: раньше список жил только в памяти, и после перезапуска
    # кнопки альбома отвечали «ссылка устарела».
    coll_id = await COLLECTIONS.open(message, url, premium=premium,
                                     saved={"title": title, "tracks": tracks})

    caption = t(
        "collection_caption", lang,
        kind=t_kind(kind, lang), title=title,
        count=len(tracks), tracks_word=t("tracks_word", lang), extra=extra,
    )
    keyboard = build_tracklist_keyboard(coll_id, tracks, 0, premium, lang)
    if cover:
        await message.answer_photo(cover, caption=caption, reply_markup=keyboard)
    else:
        await message.answer(caption, reply_markup=keyboard)


async def _handle_audio(
    message: Message, cache_url: str, source: str = None,
    meta: dict = None, fallback_query: str = None,
):
    """Тонкая обёртка: качает аудио в ответ на сообщение пользователя."""
    if source is None:
        source = cache_url
    await _do_download_audio(
        message.bot, message.chat.id, message.message_id,
        cache_url, source, lang_of(message.from_user), meta=meta, fallback_query=fallback_query,
    )


async def _resolve_audio(cache_url: str, source: str, meta: dict | None,
                         fallback_query: str | None) -> dict:
    """Откуда качать трек и с какими тегами.

    Возвращает {"candidates", "title", "performer", "duration", "cover_url", "thumb"}:
    кандидаты перебираются по очереди, если первый недоступен.
    """
    # cover_url — правильная обложка из оригинала (если есть), заменит обложку с YouTube
    cover_url = meta.get("cover") if meta else None
    target_duration = meta.get("duration") if meta else None
    info = {}

    # Если источник — поиск (Spotify): берём НЕСКОЛЬКО кандидатов, чтобы при
    # недоступности первого видео попробовать следующее. Иначе — один источник.
    if source.startswith("ytsearch"):
        query = source.split(":", 1)[1]
        # Spotify (есть meta): ищем оригинал по цепочке YT Music → SoundCloud.
        # Берём всех подходящих: лучший бывает с DRM, а перезалив рядом качается.
        found = []
        if meta:
            found = await asyncio.to_thread(
                find_track_sources, meta["performer"], meta["title"], meta["duration"]
            )
        if found:
            candidates = found
        else:
            # запасной путь — обычный поиск на YouTube (несколько кандидатов)
            candidates = await asyncio.to_thread(search_audio_candidates, query, target_duration)
            if not candidates:
                raise ValueError("not found")
        source = candidates[0]
    else:
        candidates = [source]

    if meta:
        title, performer, duration = meta["title"], meta["performer"], meta["duration"]
    else:
        drm_meta = None
        try:
            info = await asyncio.to_thread(get_video_info, source)
        except Exception as e:
            # DRM на SoundCloud — сам файл зашифрован. Берём метаданные защищённого
            # трека (название, исполнитель, длительность) и ищем его на YouTube.
            # «Video unavailable» — та же по сути беда, что и DRM: сам источник
            # отдать файл не может, и единственный путь — искать трек заново.
            gone = "DRM" in str(e) or "unavailable" in str(e).lower()
            if not (gone and fallback_query):
                raise
            # У снятого ролика метаданных не получить — тогда идём с запросом,
            # собранным заранее (см. youtube_search_query).
            try:
                drm_meta = await asyncio.to_thread(get_video_info, cache_url, True)
            except Exception:
                drm_meta = {}
            query = (
                f"{drm_meta.get('uploader') or ''} {drm_meta.get('title') or ''}".strip()
                or fallback_query
            )
            target = int(drm_meta.get("duration") or 0) or None
            source = await asyncio.to_thread(search_audio, query, target)
            candidates = [source]
            cover_url = await asyncio.to_thread(
                get_soundcloud_cover, cache_url
            ) or drm_meta.get("thumbnail")
            info = await asyncio.to_thread(get_video_info, source)

        if drm_meta:
            # точные теги — из самого SoundCloud, а не из найденного YouTube-видео
            title = drm_meta.get("title") or "Без названия"
            performer = drm_meta.get("uploader") or drm_meta.get("artist")
            duration = int(drm_meta.get("duration") or 0)
        else:
            title = info.get("track") or info.get("title") or "Без названия"
            performer = info.get("artist") or info.get("uploader") or info.get("creator")
            duration = int(info.get("duration", 0) or 0)

    # Маленькую миниатюру передаём ВСЕГДА и правильную: для прямых треков берём
    # обложку самого трека. Так мгновенно показываемая картинка точно верная,
    # а не подставленная Telegram'ом старая/чужая.
    thumb = cover_url or (info.get("thumbnail") if not meta else None)
    return {"candidates": candidates, "title": title, "performer": performer,
            "duration": duration, "cover_url": cover_url, "thumb": thumb}


async def _do_download_audio(
    bot: Bot, chat_id: int, reply_to: int,
    cache_url: str, source: str, lang: str, meta: dict = None, fallback_query: str = None,
):
    """
    Ядро скачивания одного трека (кэш + глобальный слот). БЕЗ проверки «1 на юзера» —
    её делает вызывающий (одиночная загрузка или «Скачать всё»).
    Одиночный трек качается МОЛЧА (без прогресс-бара). Общий прогресс показывает
    только «Скачать всё» (там статус «N / total»).
    """
    async def send(file_id):
        await bot.send_audio(chat_id, file_id, reply_to_message_id=reply_to)

    async def work(paths):
        # «Отправляет файл…» в шапке чата на всё время работы: поиск трека, скачивание
        # и заливка занимают секунды, а никакого статуса у одиночного трека нет вовсе.
        await chat_action.once(bot, chat_id, chat_action.DOCUMENT)
        plan = await _resolve_audio(cache_url, source, meta, fallback_query)

        # Качаем, перебирая кандидатов: если видео недоступно — пробуем следующее
        file_path = None
        for cand in plan["candidates"]:
            try:
                file_path = paths.keep(await asyncio.to_thread(
                    download_audio, cand, None, None, plan["cover_url"] is None))
                break
            except Exception:
                logger.warning("Кандидат недоступен, пробую следующий: %s", cand)
        if not file_path:
            raise ValueError("no available source")
        # Если есть точная обложка из оригинала — вшиваем её (заменяя любую чужую)
        if plan["cover_url"]:
            await asyncio.to_thread(set_metadata, file_path, plan["cover_url"],
                                    plan["title"], plan["performer"])

        thumbnail = None
        if plan["thumb"]:
            thumb_bytes = await asyncio.to_thread(make_thumbnail, plan["thumb"])
            if thumb_bytes:
                thumbnail = BufferedInputFile(thumb_bytes, filename="cover.jpg")
        sent = await bot.send_audio(
            chat_id,
            await tg_files.input_file_async(file_path, await _nice_name(file_path, quality="")),
            title=plan["title"],
            performer=plan["performer"],
            duration=plan["duration"],
            thumbnail=thumbnail,
            reply_to_message_id=reply_to,
        )
        return sent.audio.file_id if sent.audio else None

    async def on_error(e):
        await bot.send_message(chat_id, limits.friendly_error(e, lang), reply_to_message_id=reply_to)

    await job.run(cache=Cache(cache_url, "audio"), send=send, work=work, on_error=on_error,
                  lang=lang, label=f"аудио {cache_url}")


@router.callback_query(F.data.startswith("sppage:"))
async def handle_collection_page(callback: CallbackQuery):
    """Переключение страниц списка треков"""
    lang = lang_of(callback.from_user)
    _, coll_id, page_str = callback.data.split(":")
    coll = await COLLECTIONS.for_click(callback, coll_id, lang)
    if not coll:
        return
    keyboard = build_tracklist_keyboard(coll_id, coll["tracks"], int(page_str), coll.get("premium", False), lang)
    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("sptrk:"))
async def handle_collection_track(callback: CallbackQuery, bot: Bot):
    """Скачивание одного трека из альбома/плейлиста/сета"""
    lang = lang_of(callback.from_user)
    _, coll_id, idx_str = callback.data.split(":")
    coll = await COLLECTIONS.for_click(callback, coll_id, lang)
    if not coll:
        return

    # Номер трека из кнопки проверяем: «tracks[int(idx)]» на подобранных данных давал
    # не понятный отказ, а падение обработчика (ValueError/IndexError).
    tracks = coll["tracks"]
    if not idx_str.isdigit() or int(idx_str) >= len(tracks):
        logger.warning("Кнопка трека с неверным номером: %r", idx_str)
        await callback.answer(t("link_expired", lang), show_alert=True)
        return
    track = tracks[int(idx_str)]
    # Одно нажатие — один трек: раньше пять нажатий давали пять одинаковых файлов.
    if not _claim(callback.message.chat.id, coll_id, idx_str):
        await callback.answer(t("already_downloading", lang), show_alert=False)
        return
    try:
        alerts.current_request.set(f"трек из коллекции: {track['cache_url']}")  # контекст для тревог
        await callback.answer()
        await _do_download_audio(
            bot, callback.message.chat.id, callback.message.message_id,
            track["cache_url"], track["source"], lang,
            meta=track["meta"], fallback_query=track["fallback_query"],
        )
    finally:
        _unclaim(callback.message.chat.id, coll_id, idx_str)


@router.callback_query(F.data.startswith("dlall:"))
async def handle_download_all(callback: CallbackQuery, bot: Bot):
    """Premium: скачивание всех треков альбома/плейлиста по очереди"""
    lang = lang_of(callback.from_user)
    _, coll_id = callback.data.split(":")
    coll = await COLLECTIONS.for_click(callback, coll_id, lang)
    if not coll:
        return

    user_id = callback.from_user.id
    async with SessionLocal() as session:
        if not await is_premium(session, user_id):
            await callback.answer(t("premium_alert", lang), show_alert=True)
            return

    chat_id = callback.message.chat.id
    reply_to = coll.get("user_msg_id")
    tracks = coll["tracks"]
    total = len(tracks)

    # Отметка «этот человек уже качает» снимается в любом случае, даже если не пройдёт
    # отправка первого же сообщения — иначе он до перезапуска получал бы «дождись
    # текущей загрузки». Проверка и отметка – один шаг (см. handle_hdrezka_quality).
    async with job.exclusive(user_id) as mine:
        if not mine:
            await callback.answer(t("wait_current", lang), show_alert=True)
            return
        await callback.answer()
        status = await bot.send_message(
            chat_id, t("downloading_all", lang, i=0, total=total),
            reply_to_message_id=reply_to
        )
        for i, track in enumerate(tracks, 1):
            await safe_edit(status, f"{t('downloading_all', lang, i=i, total=total)}\n{track['title']}")
            alerts.current_request.set(f"трек из коллекции [все]: {track['cache_url']}")
            try:
                await _do_download_audio(
                    bot, chat_id, reply_to,
                    track["cache_url"], track["source"], lang,
                    meta=track["meta"], fallback_query=track["fallback_query"],
                )
            except Exception:
                logger.exception("Track in 'download all' failed: %s", track["title"])
        await safe_delete(status)
