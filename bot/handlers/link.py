import asyncio
import logging
import os
import uuid
from urllib.parse import urlparse, unquote
from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery, FSInputFile, BufferedInputFile,
    InputMediaPhoto, InputMediaVideo,
)
from bot.utils.platform_detector import detect_platform, Platform
from bot.keyboards.quality import build_quality_keyboard, FREE_LIMIT
from bot.keyboards.tracklist import build_tracklist_keyboard
from bot.utils.progress_bar import make_progress_bar
from bot.utils import limits
from bot.database import SessionLocal
from bot.database.repository import (
    get_cached_file_id, save_cached_file_id, increment_download, is_premium,
)
from worker.downloaders.ytdlp_wrapper import (
    get_video_info, get_available_qualities, download_video, download_shorts,
    download_audio, search_audio, search_audio_candidates, get_soundcloud_set, download_media
)
from worker.downloaders.spotify import get_track_info, get_collection_info
from worker.downloaders.instagram import download_reel, download_post, is_image
from worker.downloaders.tiktok import download_tiktok
from worker.downloaders import hdrezka
from bot.keyboards.hdrezka import (
    build_translator_keyboard, build_hdrezka_quality_keyboard,
    build_season_keyboard, build_episode_keyboard,
)
from worker.downloaders.audio_meta import set_metadata, get_soundcloud_cover, make_thumbnail

router = Router()
logger = logging.getLogger(__name__)

# url_id -> {"url": ..., "chat_id": ..., "user_msg_id": ..., "info": ...}
URL_STORE: dict[str, dict] = {}

# coll_id -> {"kind", "title", "cover", "tracks": [...]}
# где каждый трек: {"title", "cache_url", "source", "meta", "fallback_query"}
COLLECTION_STORE: dict[str, dict] = {}

# sid -> {"api", "name", "is_series", "translators", "season", "episode",
#         "chat_id", "user_msg_id", "streams": {tid: stream}}
HDREZKA_STORE: dict[str, dict] = {}

# Чтобы хранилища не росли бесконечно (утечка памяти при долгой работе),
# держим не больше последних N записей — старые выкидываем.
_STORE_CAP = 300


def _remember(store: dict, key: str, value: dict):
    store[key] = value
    if len(store) > _STORE_CAP:
        for old in list(store.keys())[: len(store) - _STORE_CAP]:
            store.pop(old, None)

# Пользователи с активной (идущей прямо сейчас) загрузкой — у каждого не больше одной
ACTIVE_DOWNLOADS: set[int] = set()


@router.message(F.text)
async def handle_link(message: Message):
    url = message.text.strip()

    if not url.startswith("http"):
        return

    platform = detect_platform(url)

    if platform == Platform.UNKNOWN:
        await message.answer("Эта ссылка пока не поддерживается.")
        return

    # Статистика популярности платформ
    async with SessionLocal() as session:
        await increment_download(session, platform.value)

    # Shorts — скачиваем сразу без лишних сообщений
    if platform == Platform.YOUTUBE_SHORTS:
        await _handle_simple_video(message, url, download_shorts, "shorts")
        return

    # Аудио (SoundCloud, YT Music) — качаем сразу в mp3 с тегами.
    # Для SoundCloud готовим запасной поиск на YouTube на случай DRM-защиты.
    if platform in (Platform.SOUNDCLOUD, Platform.YT_MUSIC):
        fallback = _soundcloud_query(url) if platform == Platform.SOUNDCLOUD else None
        await _handle_audio(message, url, fallback_query=fallback)
        return

    # Spotify-трек — читаем название/исполнителя и ищем на YouTube
    if platform == Platform.SPOTIFY:
        await _handle_spotify(message, url)
        return

    # Spotify-альбом/плейлист — показываем список треков с выбором
    if platform == Platform.SPOTIFY_COLLECTION:
        await _handle_spotify_collection(message, url)
        return

    # SoundCloud-сет (альбом/плейлист) — тоже список треков
    if platform == Platform.SOUNDCLOUD_SET:
        await _handle_soundcloud_set(message, url)
        return

    # Instagram Reel — короткое видео, качаем сразу (как Shorts)
    if platform == Platform.INSTAGRAM_REEL:
        await _handle_simple_video(message, url, download_reel, "reel")
        return

    # Instagram пост — фото, видео или карусель (отдаём альбомом)
    if platform == Platform.INSTAGRAM_POST:
        await _handle_files(message, url, download_post, "Не удалось скачать пост, возможно, он приватный.")
        return

    # TikTok — видео без водяного знака или слайдшоу (через tikwm API)
    if platform == Platform.TIKTOK:
        await _handle_files(message, url, download_tiktok, "Не удалось скачать, проверь ссылку или попробуй позже.")
        return

    # Pinterest — одно медиа (фото или видео) через yt-dlp
    if platform == Platform.PINTEREST:
        await _handle_media(message, url, "pinterest")
        return

    # PornHub shorties — это обычное видео с другим URL. Переписываем в стандартный
    # и отправляем сразу лучшим качеством (как reels/shorts).
    if platform == Platform.PORNHUB_SHORT:
        video_id = urlparse(url).path.rstrip("/").split("/")[-1]
        std_url = f"https://www.pornhub.com/view_video.php?viewkey={video_id}"
        await _handle_simple_video(message, std_url, download_shorts, "ph_short")
        return

    # YouTube-видео и PornHub — выбор качества кнопками
    if platform in (Platform.YOUTUBE_VIDEO, Platform.PORNHUB):
        await _handle_quality_video(message, url)
        return

    # HDRezka — фильм/сериал: выбор озвучки, затем качества
    if platform == Platform.HDREZKA:
        await _handle_hdrezka(message, url)
        return


async def _handle_quality_video(message: Message, url: str):
    """Получает метаданные и показывает выбор качества (YouTube, PornHub)."""
    status = await message.reply("⏳ Ищу видосик...")
    try:
        info = await asyncio.to_thread(get_video_info, url)
        available = await asyncio.to_thread(get_available_qualities, info)

        title = info.get("title", "Без названия")
        thumbnail = info.get("thumbnail")

        async with SessionLocal() as session:
            premium = await is_premium(session, message.from_user.id)

        await status.delete()
        url_id = uuid.uuid4().hex[:8]
        _remember(URL_STORE, url_id, {
            "url": url,
            "chat_id": message.chat.id,
            "user_msg_id": message.message_id,
            "info": info,  # сохраняем метаданные — не запрашиваем источник второй раз
            "premium": premium,
        })

        caption = f"{title}\n\nВыбери качество:"

        keyboard = build_quality_keyboard(url_id, available, premium)

        if thumbnail:
            await message.answer_photo(thumbnail, caption=caption, reply_markup=keyboard)
        else:
            await message.answer(caption, reply_markup=keyboard)

    except Exception:
        logger.exception("Failed to get video info")
        await _safe_edit(
            status,
            "Не удалось получить информацию о видео.\nПроверь ссылку или попробуй ещё раз."
        )


async def _handle_hdrezka(message: Message, url: str):
    """HDRezka: фильм → озвучки; сериал → сначала сезон и серия."""
    status = await message.reply("⏳ Ищу фильм/сериал...")
    try:
        api = await asyncio.to_thread(hdrezka.open_media, url)
        info = hdrezka.get_info(api, url)
    except Exception:
        logger.exception("HDRezka info failed")
        await _safe_edit(status, "Не удалось открыть страницу HDRezka, проверь ссылку.")
        return

    async with SessionLocal() as session:
        premium = await is_premium(session, message.from_user.id)

    sid = uuid.uuid4().hex[:8]
    _remember(HDREZKA_STORE, sid, {
        "api": api,  # переиспользуем объект на всех шагах — не качаем страницу заново
        "name": info["name"],
        "is_series": info["is_series"],
        "translators": info.get("translators", []),
        "seasons": info.get("seasons", []),
        "season": None,
        "episode": None,
        "thumbnail": info.get("thumbnail"),
        "premium": premium,
        "chat_id": message.chat.id,
        "user_msg_id": message.message_id,
        "streams": {},  # tid -> объект потока (кэш между «озвучкой» и «качеством»)
    })
    entry = HDREZKA_STORE[sid]
    await status.delete()

    if info["is_series"]:
        caption = f"Сериал: {info['name']}\n\nВыбери сезон:"
        keyboard = build_season_keyboard(sid, info["seasons"])
    else:
        caption = f"Фильм: {info['name']}\n\nВыбери озвучку:"
        keyboard = build_translator_keyboard(sid, info["translators"], 0)

    if entry["thumbnail"]:
        await message.answer_photo(entry["thumbnail"], caption=caption, reply_markup=keyboard)
    else:
        await message.answer(caption, reply_markup=keyboard)


@router.callback_query(F.data.startswith("hrss:"))
async def handle_hdrezka_season(callback: CallbackQuery):
    """Сезон выбран — показываем серии"""
    _, sid, season = callback.data.split(":")
    entry = HDREZKA_STORE.get(sid)
    if not entry:
        await callback.answer("Ссылка устарела, отправь её ещё раз", show_alert=True)
        return
    season = int(season)
    entry["season"] = season
    await callback.answer()
    episodes = await asyncio.to_thread(hdrezka.get_episodes, entry["api"], season)
    text = f"Сериал: {entry['name']}\nСезон {season}\n\nВыбери серию:"
    await _edit_or_caption(callback.message, text, build_episode_keyboard(sid, season, episodes))


@router.callback_query(F.data.startswith("hrep:"))
async def handle_hdrezka_episode(callback: CallbackQuery):
    """Серия выбрана — показываем озвучки этой серии"""
    _, sid, season, episode = callback.data.split(":")
    entry = HDREZKA_STORE.get(sid)
    if not entry:
        await callback.answer("Ссылка устарела, отправь её ещё раз", show_alert=True)
        return
    entry["season"] = int(season)
    entry["episode"] = int(episode)
    entry["streams"] = {}  # сменилась серия — старые потоки не подходят
    await callback.answer()
    translators = await asyncio.to_thread(
        hdrezka.get_translators, entry["api"], entry["season"], entry["episode"]
    )
    entry["translators"] = translators
    text = f"{_hdrezka_head(entry)}\n\nВыбери озвучку:"
    await _edit_or_caption(callback.message, text, build_translator_keyboard(sid, translators, 0))


@router.callback_query(F.data.startswith("hrback:"))
async def handle_hdrezka_back(callback: CallbackQuery):
    """Назад к выбору сезона"""
    _, sid = callback.data.split(":")
    entry = HDREZKA_STORE.get(sid)
    if not entry:
        await callback.answer("Ссылка устарела, отправь её ещё раз", show_alert=True)
        return
    await callback.answer()
    text = f"Сериал: {entry['name']}\n\nВыбери сезон:"
    await _edit_or_caption(callback.message, text, build_season_keyboard(sid, entry["seasons"]))


@router.callback_query(F.data.startswith("hrp:"))
async def handle_hdrezka_page(callback: CallbackQuery):
    """Пагинация списка озвучек"""
    _, sid, page = callback.data.split(":")
    entry = HDREZKA_STORE.get(sid)
    if not entry:
        await callback.answer("Ссылка устарела, отправь её ещё раз", show_alert=True)
        return
    keyboard = build_translator_keyboard(sid, entry["translators"], int(page))
    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


def _hdrezka_head(entry: dict) -> str:
    """Шапка сообщения: «Фильм/Сериал: Название (+ сезон/серия)»."""
    kind = "Сериал" if entry["is_series"] else "Фильм"
    head = f"{kind}: {entry['name']}"
    if entry["is_series"]:
        head += f"\nСезон {entry['season']}, серия {entry['episode']}"
    return head


async def _edit_or_caption(message, text: str, keyboard):
    """Редактирует сообщение: caption если есть обложка, иначе обычный текст."""
    try:
        await message.edit_caption(caption=text, reply_markup=keyboard)
    except Exception:
        try:
            await message.edit_text(text, reply_markup=keyboard)
        except Exception:
            pass


async def _show_hdrezka_quality(message, entry, sid, tid):
    """Показывает кнопки качества для выбранной озвучки."""
    tname = dict(entry["translators"]).get(tid, "")
    qualities = hdrezka.stream_qualities(entry["streams"][tid])
    keyboard = build_hdrezka_quality_keyboard(sid, tid, qualities, entry.get("premium", False))
    text = f"{_hdrezka_head(entry)}\nОзвучка: {tname}\n\nВыбери качество:"
    await _edit_or_caption(message, text, keyboard)


@router.callback_query(F.data.startswith("hrt:"))
async def handle_hdrezka_translator(callback: CallbackQuery):
    """Озвучка выбрана — получаем поток (один запрос) и показываем качества"""
    _, sid, tid = callback.data.split(":")
    entry = HDREZKA_STORE.get(sid)
    if not entry:
        await callback.answer("Ссылка устарела, отправь её ещё раз", show_alert=True)
        return

    tid = int(tid)
    await callback.answer()
    tname = dict(entry["translators"]).get(tid, "")
    # Сразу показываем «идёт загрузка», чтобы не выглядело зависшим
    await _edit_or_caption(
        callback.message, f"{_hdrezka_head(entry)}\nОзвучка: {tname}\n\n⏳ Получаю качества...", None
    )
    try:
        stream = await asyncio.to_thread(
            hdrezka.get_stream, entry["api"], tid, entry["season"], entry["episode"]
        )
    except Exception:
        logger.exception("HDRezka stream failed")
        await _edit_or_caption(callback.message, "Не удалось получить данные этой озвучки.", None)
        return

    entry["streams"][tid] = stream
    await _show_hdrezka_quality(callback.message, entry, sid, tid)


@router.callback_query(F.data.startswith("hrq:"))
async def handle_hdrezka_quality(callback: CallbackQuery, bot: Bot):
    """Качество выбрано — качаем и отправляем"""
    _, sid, tid, qidx = callback.data.split(":")
    entry = HDREZKA_STORE.get(sid)
    if not entry:
        await callback.answer("Ссылка устарела, отправь её ещё раз", show_alert=True)
        return

    tid = int(tid)
    stream = entry["streams"].get(tid)
    qualities = hdrezka.stream_qualities(stream) if stream else []
    if not stream or int(qidx) >= len(qualities):
        await callback.answer("Выбери озвучку заново", show_alert=True)
        return
    quality = qualities[int(qidx)]
    user_id = callback.from_user.id

    # Защита: качество выше 720p — только для Premium
    height = 9999 if "K" in quality.upper() else int("".join(filter(str.isdigit, quality)) or 0)
    if height > FREE_LIMIT and not entry.get("premium"):
        await callback.answer("Premium ✨", show_alert=True)
        return

    if user_id in ACTIVE_DOWNLOADS:
        await callback.answer("⏳ Сначала дождись текущей загрузки.", show_alert=True)
        return

    await callback.answer()
    await callback.message.delete()
    ACTIVE_DOWNLOADS.add(user_id)
    await limits.acquire_download()
    chat_id = entry["chat_id"]
    status = await bot.send_message(chat_id, make_progress_bar(0), reply_to_message_id=entry["user_msg_id"])

    loop = asyncio.get_running_loop()
    last_percent = [-1]

    def on_progress(percent: int):
        if percent != last_percent[0]:
            last_percent[0] = percent
            asyncio.run_coroutine_threadsafe(
                _safe_edit(status, make_progress_bar(percent)), loop
            )

    try:
        file_path = await asyncio.to_thread(
            hdrezka.download_stream, stream, quality,
            entry["name"], entry["season"], entry["episode"], on_progress,
        )
        await _safe_edit(status, "📤 Отправляю...")
        await bot.send_video(
            chat_id, FSInputFile(file_path),
            supports_streaming=True, reply_to_message_id=entry["user_msg_id"],
        )
        await status.delete()
        _cleanup(file_path)
    except Exception as e:
        logger.exception("HDRezka download failed")
        await _safe_edit(status, limits.friendly_error(e))
    finally:
        limits.release_download()
        ACTIVE_DOWNLOADS.discard(user_id)


async def _handle_simple_video(message: Message, url: str, download_fn, cache_key: str):
    """Качает короткое видео сразу (Shorts, Instagram Reel): кэш, лимит, отправка."""
    async with SessionLocal() as session:
        cached_id = await get_cached_file_id(session, url, cache_key)
    if cached_id:
        await message.reply_video(cached_id, supports_streaming=True)
        return

    if message.from_user.id in ACTIVE_DOWNLOADS:
        await message.reply("⏳ Сначала дождись текущей загрузки.")
        return

    ACTIVE_DOWNLOADS.add(message.from_user.id)
    await limits.acquire_download()
    status = await message.reply("⏳ Скачиваю...")
    try:
        file_path = await asyncio.to_thread(download_fn, url)
        sent = await message.reply_video(FSInputFile(file_path), supports_streaming=True)
        if sent.video:
            async with SessionLocal() as session:
                await save_cached_file_id(session, url, sent.video.file_id, cache_key)
        await _safe_delete(status)
        _cleanup(file_path)
    except Exception as e:
        logger.exception("%s download failed", cache_key)
        await _safe_edit(status, limits.friendly_error(e))
    finally:
        limits.release_download()
        ACTIVE_DOWNLOADS.discard(message.from_user.id)


async def _handle_media(message: Message, url: str, cache_key: str):
    """Качает одно медиа (TikTok, Pinterest) и шлёт как фото или видео — по типу файла."""
    # Кэш: в file_id храним префикс типа — "P:" фото, "V:" видео
    async with SessionLocal() as session:
        cached = await get_cached_file_id(session, url, cache_key)
    if cached:
        if cached.startswith("P:"):
            await message.reply_photo(cached[2:])
        else:
            await message.reply_video(cached[2:], supports_streaming=True)
        return

    if message.from_user.id in ACTIVE_DOWNLOADS:
        await message.reply("⏳ Сначала дождись текущей загрузки.")
        return

    ACTIVE_DOWNLOADS.add(message.from_user.id)
    await limits.acquire_download()
    status = await message.reply("⏳ Скачиваю...")
    try:
        file_path = await asyncio.to_thread(download_media, url)
        if is_image(file_path):
            sent = await message.reply_photo(FSInputFile(file_path))
            fid = "P:" + sent.photo[-1].file_id if sent.photo else None
        else:
            sent = await message.reply_video(FSInputFile(file_path), supports_streaming=True)
            fid = "V:" + sent.video.file_id if sent.video else None
        if fid:
            async with SessionLocal() as session:
                await save_cached_file_id(session, url, fid, cache_key)
        await _safe_delete(status)
        _cleanup(file_path)
    except Exception as e:
        logger.exception("%s download failed", cache_key)
        await _safe_edit(status, limits.friendly_error(e))
    finally:
        limits.release_download()
        ACTIVE_DOWNLOADS.discard(message.from_user.id)


async def _handle_files(message: Message, url: str, download_fn, error_text: str):
    """Качает набор файлов (Instagram пост, TikTok) и отдаёт фото/видео или альбомом."""
    if message.from_user.id in ACTIVE_DOWNLOADS:
        await message.reply("⏳ Сначала дождись текущей загрузки.")
        return

    ACTIVE_DOWNLOADS.add(message.from_user.id)
    await limits.acquire_download()
    status = await message.reply("⏳ Скачиваю...")
    try:
        files = await asyncio.to_thread(download_fn, url)
        if not files:
            await _safe_edit(status, "Тут нет медиа для скачивания.")
            return

        # Один файл — отправляем напрямую (фото или видео)
        if len(files) == 1:
            f = files[0]
            if is_image(f):
                await message.reply_photo(FSInputFile(f))
            else:
                await message.reply_video(FSInputFile(f), supports_streaming=True)
        else:
            # Карусель/слайдшоу — альбомами по 10 (лимит Telegram на media group)
            for chunk in _chunked(files, 10):
                media = []
                for f in chunk:
                    if is_image(f):
                        media.append(InputMediaPhoto(media=FSInputFile(f)))
                    else:
                        media.append(InputMediaVideo(media=FSInputFile(f)))
                await message.reply_media_group(media)

        await _safe_delete(status)
        for f in files:
            _cleanup(f)
    except Exception as e:
        logger.exception("%s download failed", url)
        msg = limits.friendly_error(e)
        # ошибка не распознана — даём платформенную подсказку (напр. про приватность)
        if msg.startswith("Не удалось скачать, проверь"):
            msg = error_text
        await _safe_edit(status, msg)
    finally:
        limits.release_download()
        ACTIVE_DOWNLOADS.discard(message.from_user.id)


def _chunked(items: list, size: int):
    """Разбивает список на куски по size элементов."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


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
    except Exception:
        logger.exception("Spotify metadata failed")
        await message.reply("Не удалось прочитать трек, проверь ссылку или попробуй позже.")
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


async def _handle_spotify_collection(message: Message, url: str):
    """Читает альбом/плейлист Spotify и показывает список треков с выбором"""
    try:
        coll = await asyncio.to_thread(get_collection_info, url)
    except Exception as e:
        logger.exception("Spotify collection failed")
        # Spotify с конца 2024 не отдаёт редакционные/алгоритмические плейлисты (id вида 37i9...)
        if "404" in str(e) or "Not Found" in str(e):
            await message.reply("Spotify не отдаёт этот плейлист...")
        else:
            await message.reply("Не удалось прочитать альбом/плейлист, проверь ссылку или попробуй позже.")
        return

    if not coll["tracks"]:
        await message.reply("В этом альбоме нет треков для скачивания.")
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
        message, coll["kind"], coll["title"], tracks,
        cover=coll.get("cover"), extra=f", ~{total_min} мин",
    )


async def _handle_soundcloud_set(message: Message, url: str):
    """Читает сет (альбом/плейлист) SoundCloud и показывает список треков"""
    try:
        data = await asyncio.to_thread(get_soundcloud_set, url)
    except Exception:
        logger.exception("SoundCloud set failed")
        await message.reply("Не удалось прочитать альбом/плейлист SoundCloud. Проверь ссылку.")
        return

    if not data["tracks"]:
        await message.reply("В этом альбоме нет треков для скачивания.")
        return

    # У треков SoundCloud прямые ссылки — качаем напрямую, с фолбэком на поиск при DRM
    tracks = [{
        "title": t["title"],
        "cache_url": t["url"],
        "source": t["url"],
        "meta": None,
        "fallback_query": _soundcloud_query(t["url"]),
    } for t in data["tracks"]]

    total_min = sum(t["duration"] for t in data["tracks"]) // 60
    extra = f", ~{total_min} мин" if total_min else ""
    await _show_collection(message, "Сет", data["title"], tracks, cover=data.get("cover"), extra=extra)


async def _show_collection(message: Message, kind: str, title: str, tracks: list,
                           cover: str = None, extra: str = ""):
    """Сохраняет коллекцию и показывает список треков с пагинацией"""
    async with SessionLocal() as session:
        premium = await is_premium(session, message.from_user.id)

    coll_id = uuid.uuid4().hex[:8]
    _remember(COLLECTION_STORE, coll_id, {
        "kind": kind, "title": title, "cover": cover, "tracks": tracks,
        "premium": premium, "chat_id": message.chat.id, "user_msg_id": message.message_id,
    })

    caption = (
        f"{kind}: {title}\n"
        f"{len(tracks)} треков{extra}\n\n"
        f"Выбери трек:"
    )
    keyboard = build_tracklist_keyboard(coll_id, tracks, 0, premium)
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
    await _download_and_send_audio(
        message.bot, message.chat.id, message.from_user.id, message.message_id,
        cache_url, source, meta=meta, fallback_query=fallback_query,
    )


async def _download_and_send_audio(
    bot: Bot, chat_id: int, user_id: int, reply_to: int,
    cache_url: str, source: str, meta: dict = None, fallback_query: str = None,
):
    """Скачивание одного трека с лимитом «1 загрузка на пользователя»."""
    if user_id in ACTIVE_DOWNLOADS:
        await bot.send_message(chat_id, "⏳ Сначала дождись текущей загрузки.", reply_to_message_id=reply_to)
        return
    ACTIVE_DOWNLOADS.add(user_id)
    try:
        await _do_download_audio(bot, chat_id, reply_to, cache_url, source, meta, fallback_query)
    finally:
        ACTIVE_DOWNLOADS.discard(user_id)


async def _do_download_audio(
    bot: Bot, chat_id: int, reply_to: int,
    cache_url: str, source: str, meta: dict = None, fallback_query: str = None,
):
    """
    Ядро скачивания одного трека (кэш + глобальный слот). БЕЗ проверки «1 на юзера» —
    её делает вызывающий (одиночная загрузка или «Скачать всё»).
    """
    # Кэш: уже качали — отдаём мгновенно
    async with SessionLocal() as session:
        cached_id = await get_cached_file_id(session, cache_url, "audio")
    if cached_id:
        await bot.send_audio(chat_id, cached_id, reply_to_message_id=reply_to)
        return

    # Одиночный трек качается МОЛЧА (без прогресс-бара). Общий прогресс показывает
    # только «Скачать всё» (там статус «N / total»).
    await limits.acquire_download()

    try:
        # cover_url — правильная обложка из оригинала (если есть), заменит обложку с YouTube
        cover_url = meta.get("cover") if meta else None
        target_duration = meta.get("duration") if meta else None

        # Если источник — поиск (Spotify): берём НЕСКОЛЬКО кандидатов, чтобы при
        # недоступности первого видео попробовать следующее. Иначе — один источник.
        if source.startswith("ytsearch"):
            query = source.split(":", 1)[1]
            candidates = await asyncio.to_thread(search_audio_candidates, query, target_duration)
            if not candidates:
                raise ValueError("not found")
            source = candidates[0]
        else:
            candidates = [source]

        if meta:
            title = meta["title"]
            performer = meta["performer"]
            duration = meta["duration"]
        else:
            drm_meta = None
            try:
                info = await asyncio.to_thread(get_video_info, source)
            except Exception as e:
                # DRM на SoundCloud — сам файл зашифрован. Берём метаданные защищённого
                # трека (название, исполнитель, длительность) и ищем его на YouTube.
                if "DRM" in str(e) and fallback_query:
                    drm_meta = await asyncio.to_thread(get_video_info, cache_url, True)
                    query = (
                        f"{drm_meta.get('uploader') or ''} {drm_meta.get('title') or ''}".strip()
                        or fallback_query
                    )
                    target = int(drm_meta.get("duration") or 0) or None
                    source = await asyncio.to_thread(search_audio, query, target)
                    cover_url = await asyncio.to_thread(
                        get_soundcloud_cover, cache_url
                    ) or drm_meta.get("thumbnail")
                    info = await asyncio.to_thread(get_video_info, source)
                else:
                    raise

            if drm_meta:
                # точные теги — из самого SoundCloud, а не из найденного YouTube-видео
                title = drm_meta.get("title") or "Без названия"
                performer = drm_meta.get("uploader") or drm_meta.get("artist")
                duration = int(drm_meta.get("duration") or 0)
            else:
                title = info.get("track") or info.get("title") or "Без названия"
                performer = info.get("artist") or info.get("uploader") or info.get("creator")
                duration = int(info.get("duration", 0) or 0)

        # Качаем, перебирая кандидатов: если видео недоступно — пробуем следующее
        file_path = None
        for cand in candidates:
            try:
                file_path = await asyncio.to_thread(
                    download_audio, cand, None, None, cover_url is None
                )
                break
            except Exception:
                logger.warning("Кандидат недоступен, пробую следующий: %s", cand)
        if not file_path:
            raise ValueError("no available source")
        # Если есть точная обложка из оригинала — вшиваем её (заменяя любую чужую)
        if cover_url:
            await asyncio.to_thread(set_metadata, file_path, cover_url, title, performer)

        # Маленькую миниатюру передаём ВСЕГДА и правильную: для прямых треков берём
        # обложку самого трека. Так мгновенно показываемая картинка точно верная,
        # а не подставленная Telegram'ом старая/чужая.
        thumb_url = cover_url or (info.get("thumbnail") if not meta else None)
        thumbnail = None
        if thumb_url:
            thumb_bytes = await asyncio.to_thread(make_thumbnail, thumb_url)
            if thumb_bytes:
                thumbnail = BufferedInputFile(thumb_bytes, filename="cover.jpg")
        sent = await bot.send_audio(
            chat_id,
            FSInputFile(file_path),
            title=title,
            performer=performer,
            duration=duration,
            thumbnail=thumbnail,
            reply_to_message_id=reply_to,
        )
        if sent.audio:
            async with SessionLocal() as session:
                await save_cached_file_id(session, cache_url, sent.audio.file_id, "audio")
        _cleanup(file_path)
    except Exception as e:
        logger.exception("Audio download failed")
        await bot.send_message(chat_id, limits.friendly_error(e), reply_to_message_id=reply_to)
    finally:
        limits.release_download()


@router.callback_query(F.data.startswith("quality:"))
async def handle_quality_choice(callback: CallbackQuery, bot: Bot):
    _, quality_str, url_id = callback.data.split(":", 2)
    quality = int(quality_str)

    entry = URL_STORE.get(url_id)
    if not entry:
        await callback.answer("Ссылка устарела, отправь её ещё раз", show_alert=True)
        return

    chat_id = entry["chat_id"]
    user_msg_id = entry["user_msg_id"]
    url = entry["url"]
    info = entry.get("info")
    user_id = callback.from_user.id

    # Защита: качество выше 720p — только для Premium
    if quality > FREE_LIMIT and not entry.get("premium"):
        await callback.answer("Premium ✨", show_alert=True)
        return

    # Кэш: если это качество уже качали — отдаём мгновенно (блокировку не применяем)
    async with SessionLocal() as session:
        cached_id = await get_cached_file_id(session, url, str(quality))
    if cached_id:
        await callback.answer()
        await callback.message.delete()
        await bot.send_video(
            chat_id, cached_id,
            supports_streaming=True,
            reply_to_message_id=user_msg_id
        )
        return

    # Один пользователь — одна активная загрузка. Меню не удаляем, чтобы можно было повторить.
    if user_id in ACTIVE_DOWNLOADS:
        await callback.answer("⏳ Сначала дождись текущей загрузки.", show_alert=True)
        return

    await callback.answer()
    await callback.message.delete()
    ACTIVE_DOWNLOADS.add(user_id)
    await limits.acquire_download()

    progress_msg = await bot.send_message(
        chat_id,
        make_progress_bar(0),
        reply_to_message_id=user_msg_id
    )

    loop = asyncio.get_running_loop()
    last_percent = [-1]

    def on_progress(percent: int):
        if percent != last_percent[0]:
            last_percent[0] = percent
            asyncio.run_coroutine_threadsafe(
                _safe_edit(progress_msg, make_progress_bar(percent)),
                loop
            )

    def on_postprocess():
        # ffmpeg начал склейку — показываем отдельный статус
        asyncio.run_coroutine_threadsafe(
            _safe_edit(progress_msg, "⚙️ Обрабатываю..."),
            loop
        )

    duration = int(info.get("duration", 0) or 0) if info else 0

    try:
        file_path = await asyncio.to_thread(
            download_video, url, quality, on_progress, on_postprocess
        )
        # Не удаляем статус, а показываем «Отправляю» — заливка тоже занимает время
        await _safe_edit(progress_msg, "📤 Отправляю...")
        sent = await bot.send_video(
            chat_id,
            FSInputFile(file_path),
            duration=duration,
            supports_streaming=True,  # видео можно смотреть на лету, не дожидаясь полной загрузки
            reply_to_message_id=user_msg_id
        )
        await progress_msg.delete()
        # Сохраняем file_id в кэш и убираем локальный файл
        if sent.video:
            async with SessionLocal() as session:
                await save_cached_file_id(session, url, sent.video.file_id, str(quality))
        _cleanup(file_path)
    except Exception as e:
        logger.exception("Download failed")
        text = limits.friendly_error(e)
        try:
            await progress_msg.edit_text(text)
        except Exception:
            await bot.send_message(chat_id, text)
    finally:
        limits.release_download()
        ACTIVE_DOWNLOADS.discard(user_id)


async def _safe_edit(msg, text: str):
    try:
        await msg.edit_text(text)
    except Exception:
        pass


async def _safe_delete(msg):
    try:
        await msg.delete()
    except Exception:
        pass


def _cleanup(file_path: str):
    """Удаляет локальный файл после отправки — диск не копит мусор"""
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        logger.warning(f"Не смог удалить {file_path}")


@router.callback_query(F.data.startswith("sppage:"))
async def handle_collection_page(callback: CallbackQuery):
    """Переключение страниц списка треков"""
    _, coll_id, page_str = callback.data.split(":")
    coll = COLLECTION_STORE.get(coll_id)
    if not coll:
        await callback.answer("Ссылка устарела, отправь её ещё раз", show_alert=True)
        return
    keyboard = build_tracklist_keyboard(coll_id, coll["tracks"], int(page_str), coll.get("premium", False))
    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("sptrk:"))
async def handle_collection_track(callback: CallbackQuery, bot: Bot):
    """Скачивание одного трека из альбома/плейлиста/сета"""
    _, coll_id, idx_str = callback.data.split(":")
    coll = COLLECTION_STORE.get(coll_id)
    if not coll:
        await callback.answer("Ссылка устарела, отправь её ещё раз", show_alert=True)
        return

    track = coll["tracks"][int(idx_str)]
    await callback.answer()

    await _download_and_send_audio(
        bot, callback.message.chat.id, callback.from_user.id, callback.message.message_id,
        track["cache_url"], track["source"],
        meta=track["meta"], fallback_query=track["fallback_query"],
    )


@router.callback_query(F.data.startswith("dlall:"))
async def handle_download_all(callback: CallbackQuery, bot: Bot):
    """Premium: скачивание всех треков альбома/плейлиста по очереди"""
    _, coll_id = callback.data.split(":")
    coll = COLLECTION_STORE.get(coll_id)
    if not coll:
        await callback.answer("Ссылка устарела, отправь её ещё раз", show_alert=True)
        return

    user_id = callback.from_user.id
    async with SessionLocal() as session:
        if not await is_premium(session, user_id):
            await callback.answer("Premium ✨", show_alert=True)
            return

    if user_id in ACTIVE_DOWNLOADS:
        await callback.answer("⏳ Сначала дождись текущей загрузки.", show_alert=True)
        return

    await callback.answer()
    chat_id = callback.message.chat.id
    reply_to = coll.get("user_msg_id")
    tracks = coll["tracks"]
    total = len(tracks)

    ACTIVE_DOWNLOADS.add(user_id)
    status = await bot.send_message(chat_id, f"⬇️ Скачиваю весь список: 0 / {total}", reply_to_message_id=reply_to)
    try:
        for i, track in enumerate(tracks, 1):
            await _safe_edit(status, f"⬇️ Скачиваю весь список: {i} / {total}\n{track['title']}")
            try:
                await _do_download_audio(
                    bot, chat_id, reply_to,
                    track["cache_url"], track["source"],
                    meta=track["meta"], fallback_query=track["fallback_query"],
                )
            except Exception:
                logger.exception("Track in 'download all' failed: %s", track["title"])
        await _safe_delete(status)
    finally:
        ACTIVE_DOWNLOADS.discard(user_id)


@router.callback_query(F.data.startswith("stub:"))
async def handle_stub(callback: CallbackQuery):
    await callback.answer(
        "Эта функция в разработке, скоро будет доступна 🚀",
        show_alert=True
    )
