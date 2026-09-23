import asyncio
import html
import json
import logging
import os
import uuid
from functools import partial
from urllib.parse import urlparse, unquote
from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    InputMediaPhoto, InputMediaVideo,
)
from bot.utils.platform_detector import detect_platform, Platform, extract_url
from bot.features.download.keyboards.quality import build_quality_keyboard, FREE_LIMIT
from bot.features.download.keyboards.tracklist import build_tracklist_keyboard
from bot.utils.progress_bar import make_progress_bar, ProgressThrottle
from bot.config import SHORTS_CAP_HEIGHT, DOWNLOADS_DIR
from bot.utils import limits, traffic, tg_files
from bot.utils import inflight
from bot.utils.cache_guard import send_cached_or_drop
from bot.utils import media_names
from bot.features.common import alerts
from bot.utils.i18n import t, lang_of, t_kind
from bot.database import SessionLocal
from bot.database.repository import (
    get_cached_file_id, save_cached_file_id, increment_download, is_premium,
    get_slideshow_mode, get_audio_track, get_compress_shorts,
    save_link_stash, load_link_stash,
)
from bot.features.download.downloaders.audio_extract import extract_audio_track
from bot.features.download.downloaders.ytdlp_wrapper import (
    youtube_search_query,
    get_video_info, get_available_qualities, download_video, download_shorts,
    estimate_size,
    download_audio, search_audio, search_audio_candidates, get_soundcloud_set,
    download_media, convert_gif_to_mp4,
)
from bot.features.download.downloaders.spotify import get_track_info, get_collection_info
from bot.features.download.downloaders.music_search import find_track_source
from bot.features.download.downloaders.instagram import download_reel, download_post, is_image
from bot.features.download.downloaders import hdrezka, twitter, tiktok
from bot.features.download.renderer.tweet_card import render_tweet_card
from bot.features.download.keyboards.tiktok import build_tiktok_slideshow_keyboard
from bot.features.download.keyboards.hdrezka import (
    build_translator_keyboard, build_hdrezka_quality_keyboard,
    build_season_keyboard, build_episode_keyboard,
)
from bot.features.download.downloaders.audio_meta import set_metadata, get_soundcloud_cover, make_thumbnail
from bot.features.download.downloaders.video_meta import probe_video, make_video_thumbnail

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

# sid -> {"url", "info"} — данные слайдшоу TikTok между вопросом и выбором формата
TIKTOK_STORE: dict[str, dict] = {}

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

# Короткий id -> ссылка, для inline-перехода в личку (deep link короче 64 символов)
INLINE_LINKS: dict[str, str] = {}


def stash_inline_link(url: str) -> str:
    """Сохраняет ссылку под коротким id (для кнопки-перехода из inline в личку)."""
    sid = uuid.uuid4().hex[:8]
    _remember(INLINE_LINKS, sid, url)
    return sid


@router.message(F.text)
async def handle_link(message: Message):
    # Берём из сообщения именно ССЫЛКУ, а не весь текст: рядом с ней почти всегда
    # идёт подпись, и раньше она уезжала на площадку как часть адреса.
    await process_link(message, extract_url(message.text))


async def process_link(message: Message, url: str):
    """Обработка одной ссылки. Вызывается из текстовых сообщений и из inline-перехода."""
    if not url.startswith("http"):
        return

    lang = lang_of(message.from_user)
    platform = detect_platform(url)

    # Неподдерживаемая ссылка — молчим (особенно важно в группах: не реагируем
    # на чужие/сторонние ссылки, чтобы не спамить и не отвечать невпопад).
    if platform == Platform.UNKNOWN:
        return

    # Контекст для уведомления админу о сбое (какая площадка и ссылка). Выставляем ДО
    # запуска обработки, чтобы дочерние задачи скачивания его унаследовали.
    alerts.current_request.set(f"{platform.value}: {url}")

    # Статистика популярности платформ
    async with SessionLocal() as session:
        await increment_download(session, platform.value)

    await _dispatch_platform(message, url, platform, lang)


GROUP_TYPES = ("group", "supergroup")


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


async def _dispatch_platform(message: Message, url: str, platform, lang: str):
    """Отправляет контент по платформе; каждая ветка сама завершает работу."""
    # Shorts — скачиваем сразу без лишних сообщений
    if platform == Platform.YOUTUBE_SHORTS:
        cap = await _shorts_cap(message.chat)
        await _handle_simple_video(message, url, partial(download_shorts, max_height=cap),
                                   _shorts_key("shorts", cap), lang)
        return

    # Аудио (SoundCloud, YT Music) — качаем сразу в mp3 с тегами.
    # Для SoundCloud готовим запасной поиск на YouTube на случай DRM-защиты.
    if platform in (Platform.SOUNDCLOUD, Platform.YT_MUSIC):
        # Запасной путь на случай, когда сам источник недоступен: у SoundCloud это
        # защита от копирования, у YT Music — снятые правообладателем релизы («Video
        # unavailable»). В обоих случаях трек ищется на обычном YouTube по названию.
        if platform == Platform.SOUNDCLOUD:
            fallback = _soundcloud_query(url)
        else:
            fallback = await asyncio.to_thread(youtube_search_query, url)
        await _handle_audio(message, url, fallback_query=fallback)
        return

    # Spotify-трек — читаем название/исполнителя и ищем на YouTube
    if platform == Platform.SPOTIFY:
        await _handle_spotify(message, url)
        return

    # Spotify-альбом/плейлист — показываем список треков с выбором
    if platform == Platform.SPOTIFY_COLLECTION:
        await _handle_spotify_collection(message, url, lang)
        return

    # SoundCloud-сет (альбом/плейлист) — тоже список треков
    if platform == Platform.SOUNDCLOUD_SET:
        await _handle_soundcloud_set(message, url, lang)
        return

    # Instagram Reel — короткое видео, качаем сразу (как Shorts)
    if platform == Platform.INSTAGRAM_REEL:
        cap = await _shorts_cap(message.chat)
        # Название у Reels не гарантировано (подпись автора бывает пустой), поэтому
        # имя файла строим по техническому номеру — см. _nice_name.
        await _handle_simple_video(message, url, partial(download_reel, max_height=cap),
                                   _shorts_key("reel", cap), lang, use_title=False)
        return

    # Instagram пост — фото, видео или карусель (отдаём альбомом)
    if platform == Platform.INSTAGRAM_POST:
        await _handle_files(message, url, download_post, "ig_post_failed", lang)
        return

    # TikTok — видео без водяного знака; слайдшоу спрашивает формат (видео/фото)
    if platform == Platform.TIKTOK:
        await _handle_tiktok(message, url, lang)
        return

    # Pinterest — одно медиа (фото или видео) через yt-dlp
    if platform == Platform.PINTEREST:
        await _handle_media(message, url, "pinterest", lang)
        return

    # PornHub shorties — это обычное видео с другим URL. Переписываем в стандартный
    # и отправляем сразу лучшим качеством (как reels/shorts).
    if platform == Platform.PORNHUB_SHORT:
        video_id = urlparse(url).path.rstrip("/").split("/")[-1]
        std_url = f"https://www.pornhub.com/view_video.php?viewkey={video_id}"
        # Сжатие здесь сознательно НЕ применяем: у PornHub качество пониже раздаётся
        # с задушенного узла (замер на одном ролике: 1080p — 4с, тот же ролик в 720p — 48с),
        # так что «сжатие» вышло бы медленнее и хуже одновременно. Откат по качеству при
        # сбое внутри download_shorts продолжает работать.
        await _handle_simple_video(message, std_url, download_shorts, "ph_short", lang)
        return

    # YouTube-видео и PornHub — выбор качества кнопками
    if platform in (Platform.YOUTUBE_VIDEO, Platform.PORNHUB):
        await _handle_quality_video(message, url, lang)
        return

    # HDRezka — фильм/сериал: выбор озвучки, затем качества
    if platform == Platform.HDREZKA:
        await _handle_hdrezka(message, url, lang)
        return

    # X (Twitter) — один пост: фото/видео/gif + текст (карточка-картинка позже)
    if platform == Platform.TWITTER:
        await _handle_twitter(message, url, lang)
        return


async def _maybe_send_audio_track(message: Message, url: str, platform, lang: str):
    """Если в этом чате включён тумблер «Присылать аудио к слайдшоу» — шлём отдельным
    сообщением музыку поста. Вызывается ТОЛЬКО когда слайдшоу отдано как ФОТО (у фото нет
    звука, поэтому музыку докладываем); к видео не применяется — там звук уже внутри."""
    async with SessionLocal() as session:
        if not await get_audio_track(session, message.chat.id):
            return

    # Быстрый кэш по ссылке — мгновенно и БЕЗ запроса к TikTok (частый случай:
    # та же ссылка; устойчиво к сбоям API TikTok).
    async with SessionLocal() as session:
        cached = await get_cached_file_id(session, url, "audiotrack")
    if cached:
        await message.reply_audio(cached)
        return

    # Для TikTok — запасной ключ по номеру видео (дедуп разных коротких ссылок).
    # Номер берём из данных поста (они уже в памяти после показа видео).
    cache_url = url
    if platform == Platform.TIKTOK:
        try:
            info = await asyncio.to_thread(tiktok.fetch_tiktok, url)
            cache_url = f"tt:{info['id']}"
            if cache_url != url:
                async with SessionLocal() as session:
                    cached = await get_cached_file_id(session, cache_url, "audiotrack")
                if cached:
                    await message.reply_audio(cached)
                    return
        except Exception:
            pass  # не смогли определить номер — откатываемся на ссылку

    slot = await limits.acquire(limits.LIGHT)
    path = None
    try:
        result = await asyncio.to_thread(extract_audio_track, url, platform)
        if not result:
            return  # нет звука или не удалось извлечь — молча пропускаем (это бонус)
        path, title = result
        sent = await message.reply_audio(
            tg_files.input_file(path, await _nice_name(path, quality="")), title=title)
        if sent.audio:
            async with SessionLocal() as session:
                await _tt_cache_save(session, url, cache_url, sent.audio.file_id, "audiotrack")
    except Exception:
        logger.exception("Не удалось отправить аудиодорожку")
    finally:
        # Уборка именно здесь: файл скачан ещё до отправки, и если отправка упала,
        # он оставался на диске до перезапуска бота.
        _cleanup_all(path)
        await limits.release(limits.LIGHT, slot)


async def _handle_quality_video(message: Message, url: str, lang: str):
    """Получает метаданные и показывает выбор качества (YouTube, PornHub)."""
    status = await message.reply(t("searching_video", lang))
    try:
        info = await asyncio.to_thread(get_video_info, url)
        # Идущий (или предстоящий) прямой эфир не качаем — yt-dlp запишет лишь кусок
        # с момента подключения. Просим прислать ссылку после завершения трансляции.
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            await _safe_edit(status, t("live_stream", lang))
            return
        available = await asyncio.to_thread(get_available_qualities, info)

        # «Мягкая» деградация источника: ответ пришёл, но данные неполные — исключения
        # нет, поэтому раньше такой сбой не попадал НИ в логи, НИ в алерты (слепое пятно).
        # Пустой список качеств = кнопки не покажутся, пользователь упрётся в тупик
        # «Выбери качество» без кнопок. Ловим явно: лог + тревога админу + честная ошибка.
        if not available:
            logger.warning("Пустой список качеств (деградация источника): %s", url)
            alerts.note_failure(RuntimeError(f"нет доступных качеств: {url}"))
            await _safe_edit(status, t("video_info_failed", lang))
            return

        title = info.get("title")
        if not title:                     # заголовок не извлёкся — тоже сигнал деградации
            logger.warning("Метаданные без заголовка (деградация источника): %s", url)
            title = "Без названия"
        thumbnail = _pick_thumbnail(info)

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
        # То же самое в базу, но без метаданных: кнопки в Telegram живут дольше, чем
        # память бота, и после перезапуска нажатие должно работать, а не отвечать
        # «ссылка устарела». Метаданные при таком возврате запросятся заново.
        async with SessionLocal() as session:
            await save_link_stash(session, url_id, url, message.chat.id,
                                  message.message_id, premium)

        caption = t("choose_quality", lang, title=title)

        keyboard = build_quality_keyboard(url_id, available, premium)

        # Превью — украшение, кнопки — суть. Telegram иногда отказывается забирать
        # картинку по ссылке («wrong type of the web page content») даже когда она живая
        # и обычного размера. Без запасного пути падал весь экран выбора, и человек
        # вместо кнопок получал ошибку. Теперь в худшем случае будет текст с кнопками.
        sent = False
        if thumbnail:
            try:
                await message.answer_photo(thumbnail, caption=caption, reply_markup=keyboard)
                sent = True
            except Exception as e:
                logger.info("Превью не отправилось (%s) — показываю выбор качества текстом",
                            str(e)[:80])
        if not sent:
            await message.answer(caption, reply_markup=keyboard)

    except Exception as e:
        logger.exception("Failed to get video info")
        alerts.note_failure(e)            # раньше этот путь молчал в алертах — теперь нет
        await _safe_edit(status, t("video_info_failed", lang))


async def _handle_hdrezka(message: Message, url: str, lang: str):
    """HDRezka: фильм → озвучки; сериал → сначала сезон и серия."""
    status = await message.reply(t("searching_movie", lang))
    try:
        api = await asyncio.to_thread(hdrezka.open_media, url)
        info = hdrezka.get_info(api, url)
    except Exception as e:
        logger.exception("HDRezka info failed")
        alerts.note_failure(e)            # fetch-ошибки тоже должны доходить до админа
        await _safe_edit(status, t("hdrezka_open_failed", lang))
        return

    async with SessionLocal() as session:
        premium = await is_premium(session, message.from_user.id)

    sid = uuid.uuid4().hex[:8]
    _remember(HDREZKA_STORE, sid, {
        "api": api,  # переиспользуем объект на всех шагах — не качаем страницу заново
        "url": url,  # исходная ссылка — нужна как ключ кэша file_id
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
    # В базу — то, из чего экран собирается заново после перезапуска бота: ссылка и
    # чей это запрос. Страницу HDRezka и объект сессии сохранить нельзя, но их можно
    # открыть повторно — это и делает _hdrezka_entry.
    async with SessionLocal() as session:
        await save_link_stash(session, sid, url, message.chat.id, message.message_id,
                              premium, kind="hdrezka")
    await status.delete()

    if info["is_series"]:
        caption = f"{t('word_series', lang)}: {info['name']}\n\n{t('label_choose_season', lang)}"
        keyboard = build_season_keyboard(sid, info["seasons"], lang)
    else:
        caption = f"{t('word_movie', lang)}: {info['name']}\n\n{t('label_choose_translation', lang)}"
        keyboard = build_translator_keyboard(sid, info["translators"], 0, lang)

    if entry["thumbnail"]:
        await message.answer_photo(entry["thumbnail"], caption=caption, reply_markup=keyboard)
    else:
        await message.answer(caption, reply_markup=keyboard)


async def _hdrezka_entry(sid: str) -> dict | None:
    """Экран HDRezka по его id: из памяти, а если бот перезапускался — собираем заново.

    Восстановление стоит секунд: надо снова пройти анти-бот-проверку и прочитать
    страницу. Зато кнопка работает, а не отвечает «ссылка устарела» — при том что
    сообщение с ней висит в чате и выглядит живым.

    Сезон и серию не восстанавливаем: человек выберет их теми же кнопками. Потоки
    (streams) тоже — они добываются при выборе озвучки и живут недолго.
    """
    entry = HDREZKA_STORE.get(sid)
    if entry:
        return entry

    async with SessionLocal() as session:
        saved = await load_link_stash(session, sid)
    if not saved or saved.get("kind") != "hdrezka":
        return None

    url = saved["url"]
    try:
        api = await asyncio.to_thread(hdrezka.open_media, url)
        info = await asyncio.to_thread(hdrezka.get_info, api, url)
    except Exception as e:
        logger.warning("HDRezka: не смог восстановить экран %s", sid, exc_info=True)
        alerts.note_failure(e)
        return None

    entry = {
        "api": api,
        "url": url,
        "name": info["name"],
        "is_series": info["is_series"],
        "translators": info.get("translators", []),
        "seasons": info.get("seasons", []),
        "season": None,
        "episode": None,
        "thumbnail": info.get("thumbnail"),
        "premium": saved.get("premium", False),
        "chat_id": saved["chat_id"],
        "user_msg_id": saved["user_msg_id"],
        "streams": {},
    }
    _remember(HDREZKA_STORE, sid, entry)
    logger.info("HDRezka: экран %s восстановлен после перезапуска", sid)
    return entry


@router.callback_query(F.data.startswith("hrss:"))
async def handle_hdrezka_season(callback: CallbackQuery):
    """Сезон выбран — показываем серии"""
    lang = lang_of(callback.from_user)
    _, sid, season = callback.data.split(":")
    entry = await _hdrezka_entry(sid)
    if not entry:
        await callback.answer(t("link_expired", lang), show_alert=True)
        return
    season = int(season)
    entry["season"] = season
    await callback.answer()
    episodes = await asyncio.to_thread(hdrezka.get_episodes, entry["api"], season)
    text = (f"{t('word_series', lang)}: {entry['name']}\n{t('word_season', lang)} {season}"
            f"\n\n{t('label_choose_episode', lang)}")
    await _edit_or_caption(callback.message, text, build_episode_keyboard(sid, season, episodes, lang))


@router.callback_query(F.data.startswith("hrep:"))
async def handle_hdrezka_episode(callback: CallbackQuery):
    """Серия выбрана — показываем озвучки этой серии"""
    lang = lang_of(callback.from_user)
    _, sid, season, episode = callback.data.split(":")
    entry = await _hdrezka_entry(sid)
    if not entry:
        await callback.answer(t("link_expired", lang), show_alert=True)
        return
    entry["season"] = int(season)
    entry["episode"] = int(episode)
    entry["streams"] = {}  # сменилась серия — старые потоки не подходят
    await callback.answer()
    translators = await asyncio.to_thread(
        hdrezka.get_translators, entry["api"], entry["season"], entry["episode"]
    )
    entry["translators"] = translators
    text = f"{_hdrezka_head(entry, lang)}\n\n{t('label_choose_translation', lang)}"
    await _edit_or_caption(callback.message, text, build_translator_keyboard(sid, translators, 0, lang))


@router.callback_query(F.data.startswith("hrback:"))
async def handle_hdrezka_back(callback: CallbackQuery):
    """Назад к выбору сезона"""
    lang = lang_of(callback.from_user)
    _, sid = callback.data.split(":")
    entry = await _hdrezka_entry(sid)
    if not entry:
        await callback.answer(t("link_expired", lang), show_alert=True)
        return
    await callback.answer()
    text = f"{t('word_series', lang)}: {entry['name']}\n\n{t('label_choose_season', lang)}"
    await _edit_or_caption(callback.message, text, build_season_keyboard(sid, entry["seasons"], lang))


@router.callback_query(F.data.startswith("hrp:"))
async def handle_hdrezka_page(callback: CallbackQuery):
    """Пагинация списка озвучек"""
    lang = lang_of(callback.from_user)
    _, sid, page = callback.data.split(":")
    entry = await _hdrezka_entry(sid)
    if not entry:
        await callback.answer(t("link_expired", lang), show_alert=True)
        return
    keyboard = build_translator_keyboard(sid, entry["translators"], int(page), lang)
    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


def _hdrezka_head(entry: dict, lang: str) -> str:
    """Шапка сообщения: «Фильм/Сериал: Название (+ сезон/серия)»."""
    kind = t("word_series", lang) if entry["is_series"] else t("word_movie", lang)
    head = f"{kind}: {entry['name']}"
    if entry["is_series"]:
        head += f"\n{t('word_season', lang)} {entry['season']}, {t('word_episode', lang)} {entry['episode']}"
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


async def _show_hdrezka_quality(message, entry, sid, tid, lang):
    """Показывает кнопки качества для выбранной озвучки."""
    tname = dict(entry["translators"]).get(tid, "")
    qualities = hdrezka.stream_qualities(entry["streams"][tid])
    if not qualities:                 # поток без качеств — раньше был молчаливый тупик
        logger.warning("HDRezka: пустой список качеств (деградация): %s", entry.get("name"))
        alerts.note_failure(RuntimeError(f"HDRezka без качеств: {entry.get('name')}"))
        await _edit_or_caption(message, t("translation_failed", lang), None)
        return
    keyboard = build_hdrezka_quality_keyboard(sid, tid, qualities, entry.get("premium", False))
    text = (f"{_hdrezka_head(entry, lang)}\n{t('label_translation', lang, name=tname)}"
            f"\n\n{t('label_choose_quality', lang)}")
    await _edit_or_caption(message, text, keyboard)


@router.callback_query(F.data.startswith("hrt:"))
async def handle_hdrezka_translator(callback: CallbackQuery):
    """Озвучка выбрана — получаем поток (один запрос) и показываем качества"""
    lang = lang_of(callback.from_user)
    _, sid, tid = callback.data.split(":")
    entry = await _hdrezka_entry(sid)
    if not entry:
        await callback.answer(t("link_expired", lang), show_alert=True)
        return

    alerts.current_request.set(f"hdrezka: {entry.get('name','')} — {entry.get('url','')}")
    tid = int(tid)
    await callback.answer()
    tname = dict(entry["translators"]).get(tid, "")
    # Сразу показываем «идёт загрузка», чтобы не выглядело зависшим
    await _edit_or_caption(
        callback.message,
        f"{_hdrezka_head(entry, lang)}\n{t('label_translation', lang, name=tname)}\n\n{t('getting_qualities', lang)}",
        None,
    )
    try:
        stream = await asyncio.to_thread(
            hdrezka.get_stream, entry["api"], tid, entry["season"], entry["episode"]
        )
    except Exception as e:
        logger.exception("HDRezka stream failed")
        alerts.note_failure(e)
        await _edit_or_caption(callback.message, t("translation_failed", lang), None)
        return

    entry["streams"][tid] = stream
    await _show_hdrezka_quality(callback.message, entry, sid, tid, lang)


@router.callback_query(F.data.startswith("hrq:"))
async def handle_hdrezka_quality(callback: CallbackQuery, bot: Bot):
    """Качество выбрано — качаем и отправляем"""
    lang = lang_of(callback.from_user)
    _, sid, tid, qidx = callback.data.split(":")
    entry = await _hdrezka_entry(sid)
    if not entry:
        await callback.answer(t("link_expired", lang), show_alert=True)
        return

    alerts.current_request.set(f"hdrezka: {entry.get('name','')} — {entry.get('url','')}")
    tid = int(tid)
    stream = entry["streams"].get(tid)
    qualities = hdrezka.stream_qualities(stream) if stream else []
    if not stream or int(qidx) >= len(qualities):
        await callback.answer(t("reselect_translation", lang), show_alert=True)
        return
    quality = qualities[int(qidx)]
    user_id = callback.from_user.id

    # Защита: качество выше 720p — только для Premium
    height = 9999 if "K" in quality.upper() else int("".join(filter(str.isdigit, quality)) or 0)
    if height > FREE_LIMIT and not entry.get("premium"):
        await callback.answer(t("premium_alert", lang), show_alert=True)
        return

    # Кэш: этот фильм/серию в этой озвучке и качестве уже качали — отдаём мгновенно,
    # без повторного скачивания (ключ = ссылка + озвучка + сезон + серия, качество — отдельно).
    cache_url = f"{entry.get('url', '')}|hr|{tid}|{entry['season']}|{entry['episode']}"
    async with SessionLocal() as session:
        cached_id = await get_cached_file_id(session, cache_url, quality)
    if cached_id:
        await callback.answer()
        await callback.message.delete()
        await bot.send_video(
            entry["chat_id"], cached_id,
            supports_streaming=True,
            reply_to_message_id=entry["user_msg_id"],
        )
        return

    if user_id in ACTIVE_DOWNLOADS:
        await callback.answer(t("wait_current", lang), show_alert=True)
        return

    await callback.answer()
    await callback.message.delete()
    ACTIVE_DOWNLOADS.add(user_id)
    slot = await limits.acquire(limits.HEAVY)
    # Всё, что после занятия слота, — строго внутри try: 23.09.2026 бот замолчал на
    # ссылки насовсем именно здесь. Отправка полоски прогресса стояла ВНЕ защищённого
    # блока, она не прошла (исходное сообщение к тому времени удалили), слот остался
    # занятым навсегда, и следующие загрузки встали в очередь за мёртвым держателем.
    status = None
    file_path = None
    try:
        chat_id = entry["chat_id"]
        status = await bot.send_message(chat_id, make_progress_bar(0),
                                        reply_to_message_id=entry["user_msg_id"])

        loop = asyncio.get_running_loop()
        # Полоску обновляем не чаще раза в пару секунд: правка на каждый процент — это
        # до сотни правок за загрузку, и Telegram на такую частоту отвечает «подожди».
        throttle = ProgressThrottle()

        def on_progress(percent: int):
            if throttle.should_send(percent):
                asyncio.run_coroutine_threadsafe(
                    _safe_edit(status, make_progress_bar(percent)), loop
                )

        limits.check_disk_space(DOWNLOADS_DIR)      # фильм весит гигабайты (см. выше)
        file_path = await asyncio.to_thread(
            hdrezka.download_stream, stream, quality,
            entry["name"], entry["season"], entry["episode"], on_progress,
        )
        await _safe_edit(status, t("uploading", lang))
        hr_title = entry["name"]
        if entry.get("season") and entry.get("episode"):
            hr_title = f"{hr_title} S{entry['season']:02d}E{entry['episode']:02d}"
        sent = await bot.send_video(
            chat_id,
            tg_files.input_file(file_path, tg_files.display_name(
                hr_title, str(quality or ""), os.path.splitext(file_path)[1] or ".mp4")),
            reply_to_message_id=entry["user_msg_id"],
            **await _video_kwargs(file_path),
        )
        await status.delete()
        # Сохраняем file_id в кэш — следующему такой же фильм отдадим без скачивания
        if sent.video:
            async with SessionLocal() as session:
                await save_cached_file_id(session, cache_url, sent.video.file_id, quality)
    except Exception as e:
        logger.exception("HDRezka download failed")
        if status is not None:
            await _safe_edit(status, limits.friendly_error(e, lang))
    finally:
        # Фильм весит гигабайты: оставить его на диске из-за сбоя отправки — самый
        # дорогой из возможных мусоров.
        _cleanup_all(file_path)
        await limits.release(limits.HEAVY, slot)
        ACTIVE_DOWNLOADS.discard(user_id)


async def _handle_simple_video(message: Message, url: str, download_fn, cache_key: str, lang: str,
                               use_title: bool = True):
    """Качает короткое видео сразу (Shorts, Instagram Reel): кэш, лимит, отправка.
    Одну и ту же ссылку качает только один запрос — остальные ждут и берут из кэша
    (см. bot.utils.inflight)."""
    async def get_cached():
        async with SessionLocal() as session:
            return await get_cached_file_id(session, url, cache_key)

    async def send_cached(cached_id):
        await message.reply_video(cached_id, supports_streaming=True)

    async def produce():
        # Лёгкие задачи не ограничиваем «одна за раз» — можно кидать подряд, общий
        # лимит (limits.LIGHT) сам поставит лишние в очередь.
        slot = await limits.acquire_or_tell(limits.LIGHT, message, lang)
        file_path = None
        try:
            file_path = await asyncio.to_thread(download_fn, url)
            sent = await message.reply_video(
                tg_files.input_file(file_path, await _nice_name(file_path, use_title=use_title)),
                **await _video_kwargs(file_path))
            if sent.video:
                async with SessionLocal() as session:
                    await save_cached_file_id(session, url, sent.video.file_id, cache_key)
        except Exception as e:
            logger.exception("%s download failed", cache_key)
            await message.reply(limits.friendly_error(e, lang))
        finally:
            _cleanup_all(file_path)
            await limits.release(limits.LIGHT, slot)

    await inflight.deduped(url, cache_key, get_cached, send_cached, produce)


async def _handle_media(message: Message, url: str, cache_key: str, lang: str):
    """Качает одно медиа (TikTok, Pinterest) и шлёт как фото/гиф/видео — по типу файла.
    Дедуп по ссылке: параллельные запросы одной ссылки не качают повторно (inflight)."""
    # Кэш: в file_id храним префикс типа — "P:" фото, "A:" гиф, "V:" видео
    async def get_cached():
        async with SessionLocal() as session:
            return await get_cached_file_id(session, url, cache_key)

    async def send_cached(cached):
        if cached.startswith("P:"):
            await message.reply_photo(cached[2:])
        elif cached.startswith("A:"):
            await message.reply_animation(cached[2:])
        else:
            await message.reply_video(cached[2:], supports_streaming=True)

    async def produce():
        slot = await limits.acquire_or_tell(limits.LIGHT, message, lang)
        file_path = mp4 = None
        try:
            file_path = await asyncio.to_thread(download_media, url)
            if file_path.lower().endswith(".gif"):
                # GIF → чистый mp4 (без грубой авто-конвертации Telegram), шлём анимацией
                mp4 = await asyncio.to_thread(convert_gif_to_mp4, file_path)
                sent = await message.reply_animation(tg_files.input_file(mp4, await _nice_name(mp4, quality="", use_title=False)))
                fid = "A:" + sent.animation.file_id if sent.animation else None
            elif is_image(file_path):
                sent = await message.reply_photo(tg_files.input_file(file_path, await _nice_name(file_path, quality="", use_title=False)))
                fid = "P:" + sent.photo[-1].file_id if sent.photo else None
            else:
                sent = await message.reply_video(
                tg_files.input_file(file_path, await _nice_name(file_path, use_title=False)),
                **await _video_kwargs(file_path))
                fid = "V:" + sent.video.file_id if sent.video else None
            if fid:
                async with SessionLocal() as session:
                    await save_cached_file_id(session, url, fid, cache_key)
        except Exception as e:
            logger.exception("%s download failed", cache_key)
            await message.reply(limits.friendly_error(e, lang))
        finally:
            # mp4 — результат конвертации гифки; если он совпал с исходником, повторная
            # уборка того же пути безвредна (файла уже нет).
            _cleanup_all(file_path, mp4 if mp4 != file_path else None)
            await limits.release(limits.LIGHT, slot)

    await inflight.deduped(url, cache_key, get_cached, send_cached, produce)


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


async def _handle_files(message: Message, url: str, download_fn, error_key: str, lang: str):
    """Качает набор файлов (Instagram пост, TikTok) и отдаёт фото/видео или альбомом.
    Дедуп по ссылке: параллельные запросы одной ссылки не качают повторно (inflight)."""
    async def get_cached():
        # Кэш: этот пост/карусель уже качали — переотправляем мгновенно, без скачивания
        async with SessionLocal() as session:
            return await get_cached_file_id(session, url, "post")

    async def send_cached(cached):
        await _send_cached_post(message, cached)

    async def produce():
        slot = await limits.acquire_or_tell(limits.LIGHT, message, lang)
        files = None
        try:
            files = await asyncio.to_thread(download_fn, url)
            if not files:
                # Пустой результат у поста/карусели = обычно деградация источника, а не
                # реально пустой пост. Раньше молчали — теперь видно в логах и в алерте.
                logger.warning("%s: пустой результат (нет медиа) — %s", error_key, url)
                alerts.note_failure(RuntimeError(f"пустой результат ({error_key}): {url}"))
                await message.reply(t("no_media", lang))
                return

            tokens = await _send_media_files(message, files, lang)

            # Сохраняем набор в кэш — следующему такой же пост отдадим без скачивания
            if tokens:
                async with SessionLocal() as session:
                    await save_cached_file_id(session, url, "\n".join(tokens), "post")

        except Exception as e:
            logger.exception("%s download failed", url)
            msg = limits.friendly_error(e, lang)
            # ошибка не распознана — даём платформенную подсказку (напр. про приватность)
            if msg == t("generic_dl_failed", lang):
                msg = t(error_key, lang)
            await message.reply(msg)
        finally:
            _cleanup_all(files)
            await limits.release(limits.LIGHT, slot)

    await inflight.deduped(url, "post", get_cached, send_cached, produce)


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
            sent = await message.reply_photo(tg_files.input_file(f, await _nm(f, 0)))
            if sent.photo:
                tokens.append("P:" + sent.photo[-1].file_id)
        else:
            sent = await message.reply_video(tg_files.input_file(f, await _nm(f, 0)),
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
                    media.append(InputMediaPhoto(media=tg_files.input_file(f, name)))
                else:
                    media.append(InputMediaVideo(media=tg_files.input_file(f, name),
                                                 **await _video_kwargs(f)))
            sent_msgs = await message.reply_media_group(media)
            for m in sent_msgs:
                if m.photo:
                    tokens.append("P:" + m.photo[-1].file_id)
                elif m.video:
                    tokens.append("V:" + m.video.file_id)
    return tokens


async def _tt_cache_save(session, url: str, cache_url: str, value: str, cache_key: str):
    """Сохраняем file_id по ДВУМ ключам: по ссылке (мгновенный повтор той же ссылки
    без запроса к TikTok) и по номеру видео (дедуп разных коротких ссылок)."""
    await save_cached_file_id(session, cache_url, value, cache_key)
    if url != cache_url:
        await save_cached_file_id(session, url, value, cache_key)


async def _handle_tiktok(message: Message, url: str, lang: str):
    """TikTok: обычное видео — сразу; слайдшоу — спрашиваем формат (видео/фото)."""
    # Быстрый кэш ПО ССЫЛКЕ — мгновенно и БЕЗ запроса к TikTok. Частый случай:
    # переслали ту же ссылку. Устойчиво к сбоям API TikTok (он иногда отвечает
    # ошибкой на частые запросы). Проверяем все возможные форматы поста.
    async with SessionLocal() as session:
        for ck in ("tt_auto", "tt_video", "tt_photos"):
            cached = await get_cached_file_id(session, url, ck)
            if cached:
                break
        else:
            cached, ck = None, None
    # Отправляем ВНЕ сессии базы: если расписка окажется мёртвой, её надо удалить
    # своим запросом, а вложенная сессия на том же файле — лишний повод для блокировки.
    if cached and await send_cached_or_drop(
            lambda: _send_cached_post(message, cached), url, ck):
        return

    # По ссылке не нашли — узнаём данные поста (запрос к TikTok, кэшируется в памяти):
    # из них берём НАСТОЯЩИЙ номер видео — по нему кэшируем как запасной ключ.
    # При включённом сжатии HD-вариант не просим: сервис готовит его дольше, а мы всё
    # равно возьмём обычное качество. Настройку читаем заранее, до запроса.
    want_hd = await _shorts_cap(message.chat) is None
    try:
        info = await asyncio.to_thread(tiktok.fetch_tiktok, url, want_hd)
    except Exception as e:
        logger.exception("TikTok fetch failed")
        await message.reply(limits.friendly_error(e, lang))
        return

    # Ключ кэша — по номеру видео (info["id"]), а не по тексту ссылки. TikTok на одно
    # и то же видео выдаёт РАЗНЫЕ короткие ссылки (vt.tiktok.com/…); по тексту они
    # выглядят разными и раньше качались повторно. По номеру видео — один раз.
    cache_url = f"tt:{info['id']}"

    # Слайдшоу — по настройке /setconfig этого чата: video (сразу видео), photos (сразу
    # фото) или ask (кнопки выбора; их слушает только приславший ссылку). Дефолт зависит
    # от типа чата: в группе — video, в личке — ask (там выбор удобнее по умолчанию).
    compress = False          # у слайдшоу выбирать нечего — сжатие только для видео
    if info["kind"] == "slideshow":
        default_mode = "ask" if message.chat.type == "private" else "video"
        async with SessionLocal() as session:
            ss_mode = await get_slideshow_mode(session, message.chat.id, default=default_mode)

        if ss_mode == "ask":
            sid = uuid.uuid4().hex[:8]
            # запоминаем автора — в группе кнопки слушаются только его
            # Тот же приём, что у HDRezka: в базу — ссылка и кто спросил, остальное
            # (данные поста) перезапрашивается у TikTok, это быстро.
            async with SessionLocal() as session:
                await save_link_stash(session, sid, url, message.chat.id, message.message_id,
                                      kind="tiktok",
                                      payload={"cache_url": cache_url,
                                               "owner": message.from_user.id})
            _remember(TIKTOK_STORE, sid, {"url": url, "cache_url": cache_url,
                                          "info": info, "owner": message.from_user.id})
            await message.reply(
                t("tt_slideshow_ask", lang),
                reply_markup=build_tiktok_slideshow_keyboard(sid, lang),
            )
            return
        if ss_mode == "photos":
            mode, cache_key = "photos", "tt_photos"
        else:
            mode, cache_key = "video", "tt_video"
    else:
        # Обычное видео / Live. Сжатие применимо только здесь (у слайдшоу выбирать
        # нечего), поэтому и ключ кэша разделяем только для этой ветки.
        compress = not want_hd
        mode, cache_key = "auto", ("tt_auto_c" if compress else "tt_auto")

    # Кэш по номеру видео + дедуп: параллельные запросы одного видео (в т.ч. с разными
    # короткими ссылками) ждут ведущего и берут готовое из кэша.
    async def get_cached():
        async with SessionLocal() as session:
            return await get_cached_file_id(session, cache_url, cache_key)

    async def send_cached(cached):
        await _send_cached_post(message, cached)

    async def produce():
        slot = await limits.acquire_or_tell(limits.LIGHT, message, lang)
        files = None
        try:
            files = await asyncio.to_thread(tiktok.download_from, info, mode, compress)
            tokens = await _send_media_files(message, files, lang)
            if tokens:
                async with SessionLocal() as session:
                    await _tt_cache_save(session, url, cache_url, "\n".join(tokens), cache_key)
            if mode == "photos":     # у фото нет звука — доложим музыку слайдшоу (если вкл)
                await _maybe_send_audio_track(message, url, Platform.TIKTOK, lang)
        except Exception as e:
            logger.exception("TikTok download failed")
            await message.reply(limits.friendly_error(e, lang))
        finally:
            _cleanup_all(files)
            await limits.release(limits.LIGHT, slot)

    await inflight.deduped(cache_url, cache_key, get_cached, send_cached, produce)


async def _tiktok_entry(sid: str) -> dict | None:
    """Экран выбора «видео или фото» по его id — из памяти или заново из базы.

    Данные поста (ссылки на кадры и звук) живут у TikTok недолго, поэтому мы их не
    храним, а перезапрашиваем: это один запрос к их API.
    """
    async with SessionLocal() as session:
        saved = await load_link_stash(session, sid)
    if not saved or saved.get("kind") != "tiktok":
        return None
    try:
        info = await asyncio.to_thread(tiktok.fetch_tiktok, saved["url"])
    except Exception:
        logger.warning("TikTok: не смог восстановить экран %s", sid, exc_info=True)
        return None
    entry = {"url": saved["url"], "cache_url": saved.get("cache_url") or saved["url"],
             "info": info, "owner": saved.get("owner")}
    _remember(TIKTOK_STORE, sid, entry)
    logger.info("TikTok: экран %s восстановлен после перезапуска", sid)
    return entry


@router.callback_query(F.data.startswith("ttdl:"))
async def handle_tiktok_slideshow(callback: CallbackQuery):
    """Выбран формат слайдшоу TikTok: 'video' (со звуком) или 'photos' (отдельные фото)."""
    lang = lang_of(callback.from_user)
    _, mode, sid = callback.data.split(":")
    entry = TIKTOK_STORE.get(sid)
    if not entry:
        entry = await _tiktok_entry(sid)
    if not entry:
        await callback.answer(t("link_expired", lang), show_alert=True)
        return

    # Кнопки слушаются только у того, кто прислал ссылку (важно для групп в режиме
    # «Выбор»). Чужое нажатие тихо гасим — «часики» на кнопке уберутся, скачивание нет.
    if callback.from_user.id != entry.get("owner"):
        await callback.answer()
        return

    url, info = entry["url"], entry["info"]
    alerts.current_request.set(f"tiktok слайдшоу [{mode}]: {url}")  # контекст для тревог
    # Ключ по номеру видео (как в _handle_tiktok): устойчив к разным коротким ссылкам.
    cache_url = entry.get("cache_url") or f"tt:{info['id']}"
    cache_key = "tt_" + mode  # tt_video / tt_photos

    # Отвечаем на исходное сообщение пользователя, а не на своё с кнопками: своё мы
    # тут же удаляем, и ответ на него повис бы с пометкой «Удалённое сообщение».
    # Вопрос с кнопками отправлен реплаем, поэтому оригинал лежит в reply_to_message.
    target = callback.message.reply_to_message or callback.message

    # Кэш выбранного формата — отдаём мгновенно (сначала по ссылке, потом по номеру)
    async with SessionLocal() as session:
        cached = (await get_cached_file_id(session, url, cache_key)
                  or await get_cached_file_id(session, cache_url, cache_key))
    if cached:
        await callback.answer()
        await _send_cached_post(target, cached)
        await _safe_delete(callback.message)
        return

    await callback.answer()

    slot = await limits.acquire(limits.LIGHT)
    files = None
    try:
        files = await asyncio.to_thread(tiktok.download_from, info, mode)
        tokens = await _send_media_files(target, files, lang)
        if tokens:
            async with SessionLocal() as session:
                await _tt_cache_save(session, url, cache_url, "\n".join(tokens), cache_key)
        await _safe_delete(callback.message)  # убираем сообщение с кнопками
        if mode == "photos":     # выбрали «Фото» — у них нет звука, доложим музыку (если вкл)
            await _maybe_send_audio_track(target, url, Platform.TIKTOK, lang)
    except Exception as e:
        logger.exception("TikTok slideshow download failed")
        await _safe_edit(callback.message, limits.friendly_error(e, lang))
    finally:
        _cleanup_all(files)
        await limits.release(limits.LIGHT, slot)


# Telegram: подпись к медиа — максимум 1024 символа (у обычного текста 4096).
TWEET_CAPTION_MAX = 1024


async def _handle_twitter(message: Message, url: str, lang: str):
    """X (Twitter): один пост. Три случая в одном потоке (+ кэш по ссылке):
      • есть медиа        → фото/видео/gif + текст подписью;
      • чисто текст       → карточка-скриншот твита;
      • цитата-твит       → карточка + медиа цитаты + текст цитаты «цитатой» снизу.
    """
    # Кэш: этот твит уже отправляли — мгновенно переотправляем по file_id
    cached = await _twitter_cache_get(url)
    if cached and await send_cached_or_drop(
            lambda: _send_cached_tweet(message, cached), url, "tw"):
        return

    try:
        tweet = await asyncio.to_thread(twitter.get_tweet, url)
    except Exception as e:
        logger.exception("Twitter fetch failed")
        await message.reply(limits.friendly_error(e, lang))
        return

    slot = await limits.acquire_or_tell(limits.LIGHT, message, lang)
    paths: list[str] = []
    try:
        items, caption, parse_mode = await _build_twitter_plan(tweet)
        paths = [it["path"] for it in items]

        if not items:
            # карточка не нарисовалась — отдаём хотя бы текст
            await message.reply(caption or t("no_media", lang))
            return

        tokens = await _send_twitter(message, items, caption, parse_mode)
        if tokens:
            await _twitter_cache_save(url, tokens, caption, parse_mode)
    except Exception as e:
        logger.exception("Twitter handling failed")
        await message.reply(limits.friendly_error(e, lang))
    finally:
        await limits.release(limits.LIGHT, slot)
        for p in paths:
            _cleanup(p)


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
        qfiles = await asyncio.to_thread(twitter.download_media, quote["media"], tweet["id"] + "_q")
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
        f = tg_files.input_file(it["path"], await _nm(it["path"], it["kind"], 0))
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
        f = tg_files.input_file(it["path"], await _nm(it["path"], it["kind"], i))
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


async def _twitter_cache_get(url: str) -> dict | None:
    """Достаёт сохранённый твит из кэша (или None). Значение — JSON с токенами и подписью."""
    async with SessionLocal() as session:
        raw = await get_cached_file_id(session, url, "x")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


async def _twitter_cache_save(url: str, tokens: list[dict], caption: str | None,
                              parse_mode: str | None):
    payload = json.dumps({"items": tokens, "caption": caption, "pm": parse_mode})
    async with SessionLocal() as session:
        await save_cached_file_id(session, url, payload, "x")


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
        message, coll["kind"], coll["title"], tracks, lang,
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
    await _show_collection(message, "Сет", data["title"], tracks, lang, cover=data.get("cover"), extra=extra)


async def _show_collection(message: Message, kind: str, title: str, tracks: list, lang: str,
                           cover: str = None, extra: str = ""):
    """Сохраняет коллекцию и показывает список треков с пагинацией"""
    async with SessionLocal() as session:
        premium = await is_premium(session, message.from_user.id)

    coll_id = uuid.uuid4().hex[:8]
    _remember(COLLECTION_STORE, coll_id, {
        "kind": kind, "title": title, "cover": cover, "tracks": tracks,
        "premium": premium, "chat_id": message.chat.id, "user_msg_id": message.message_id,
    })

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
    await _download_and_send_audio(
        message.bot, message.chat.id, message.from_user.id, message.message_id,
        cache_url, source, lang_of(message.from_user), meta=meta, fallback_query=fallback_query,
    )


async def _download_and_send_audio(
    bot: Bot, chat_id: int, user_id: int, reply_to: int,
    cache_url: str, source: str, lang: str, meta: dict = None, fallback_query: str = None,
):
    """Скачивание одного трека. Ограничение «одна за раз» снято — музыка лёгкая,
    общий лимит (limits.LIGHT внутри) сам ставит лишние треки в очередь."""
    await _do_download_audio(bot, chat_id, reply_to, cache_url, source, lang, meta, fallback_query)


async def _do_download_audio(
    bot: Bot, chat_id: int, reply_to: int,
    cache_url: str, source: str, lang: str, meta: dict = None, fallback_query: str = None,
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
    slot = await limits.acquire(limits.LIGHT)

    file_path = None
    try:
        # cover_url — правильная обложка из оригинала (если есть), заменит обложку с YouTube
        cover_url = meta.get("cover") if meta else None
        target_duration = meta.get("duration") if meta else None

        # Если источник — поиск (Spotify): берём НЕСКОЛЬКО кандидатов, чтобы при
        # недоступности первого видео попробовать следующее. Иначе — один источник.
        if source.startswith("ytsearch"):
            query = source.split(":", 1)[1]
            # Spotify (есть meta): ищем оригинал по цепочке YT Music → SoundCloud.
            found = None
            if meta:
                found = await asyncio.to_thread(
                    find_track_source, meta["performer"], meta["title"], meta["duration"]
                )
            if found:
                candidates = [found]
            else:
                # запасной путь — обычный поиск на YouTube (несколько кандидатов)
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
                # «Video unavailable» — та же по сути беда, что и DRM: сам источник
                # отдать файл не может, и единственный путь — искать трек заново.
                gone = "DRM" in str(e) or "unavailable" in str(e).lower()
                if gone and fallback_query:
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
            tg_files.input_file(file_path, await _nice_name(file_path, quality="")),
            title=title,
            performer=performer,
            duration=duration,
            thumbnail=thumbnail,
            reply_to_message_id=reply_to,
        )
        if sent.audio:
            async with SessionLocal() as session:
                await save_cached_file_id(session, cache_url, sent.audio.file_id, "audio")
    except Exception as e:
        logger.exception("Audio download failed")
        await bot.send_message(chat_id, limits.friendly_error(e, lang), reply_to_message_id=reply_to)
    finally:
        _cleanup_all(file_path)
        await limits.release(limits.LIGHT, slot)


@router.callback_query(F.data.startswith("quality:"))
async def handle_quality_choice(callback: CallbackQuery, bot: Bot):
    lang = lang_of(callback.from_user)
    _, quality_str, url_id = callback.data.split(":", 2)
    quality = int(quality_str)

    entry = URL_STORE.get(url_id)
    if not entry:
        # В памяти нет — бот перезапускался. Поднимаем ссылку из базы: метаданных там
        # нет, поэтому качество скачается на полторы секунды дольше, зато кнопка живая.
        async with SessionLocal() as session:
            entry = await load_link_stash(session, url_id)
    if not entry:
        await callback.answer(t("link_expired", lang), show_alert=True)
        return

    chat_id = entry["chat_id"]
    user_msg_id = entry["user_msg_id"]
    url = entry["url"]
    info = entry.get("info")
    user_id = callback.from_user.id
    # Контекст для тревог о сбоях: этот сбой пришёл из колбэка (кнопки), а не из входящей
    # ссылки, поэтому ставим контекст здесь — иначе в алерте был бы прочерк «—».
    alerts.current_request.set(f"видео {quality}p: {url}")

    # Защита: качество выше 720p — только для Premium
    if quality > FREE_LIMIT and not entry.get("premium"):
        await callback.answer(t("premium_alert", lang), show_alert=True)
        return

    # Кэш: если это качество уже качали — отдаём мгновенно (блокировку не применяем)
    async with SessionLocal() as session:
        cached_id = await get_cached_file_id(session, url, str(quality))
    if cached_id:
        await callback.answer()
        await callback.message.delete()

        async def _resend():
            await bot.send_video(
                chat_id, cached_id,
                supports_streaming=True,
                reply_to_message_id=user_msg_id
            )

        if await send_cached_or_drop(_resend, url, str(quality)):
            return
        # Расписка оказалась мёртвой — качаем это качество заново, как в первый раз.

    # Размер известен заранее — не тратим полчаса и гигабайты домашнего канала на файл,
    # который Telegram всё равно не примет. Оценка приблизительная, поэтому берём запас:
    # предупреждаем только при явном превышении потолка.
    if info:
        approx = estimate_size(info, quality)
        if approx and approx > limits.MAX_FILE_BYTES:
            await callback.answer(
                t("too_big_before", lang, size=f"{approx / 1024 / 1024 / 1024:.1f}"),
                show_alert=True)
            return

    # Один пользователь — одна активная загрузка. Меню не удаляем, чтобы можно было повторить.
    if user_id in ACTIVE_DOWNLOADS:
        await callback.answer(t("wait_current", lang), show_alert=True)
        return

    await callback.answer()
    await callback.message.delete()
    ACTIVE_DOWNLOADS.add(user_id)
    slot = await limits.acquire(limits.HEAVY)

    # Всё, что после занятия слота, — строго внутри try. Отправка полоски прогресса
    # стояла ВНЕ него, и если она не проходила (сообщение человека удалили, у бота
    # забрали право писать), слот оставался занятым навсегда, а следующие загрузки
    # молча вставали за мёртвым держателем — см. 23.09.2026.
    progress_msg = None
    file_path = None
    try:
        progress_msg = await bot.send_message(
            chat_id,
            make_progress_bar(0),
            reply_to_message_id=user_msg_id
        )

        loop = asyncio.get_running_loop()
        throttle = ProgressThrottle()      # не чаще раза в пару секунд (см. выше)

        def on_progress(percent: int):
            if throttle.should_send(percent):
                asyncio.run_coroutine_threadsafe(
                    _safe_edit(progress_msg, make_progress_bar(percent)),
                    loop
                )

        def on_postprocess():
            # ffmpeg начал склейку — показываем отдельный статус
            asyncio.run_coroutine_threadsafe(
                _safe_edit(progress_msg, t("processing", lang)),
                loop
            )

        duration = int(info.get("duration", 0) or 0) if info else 0

        # Место на диске проверяем ДО загрузки: на забитом диске yt-dlp и ffmpeg падают
        # с невнятным «errno 28», а человек видит бессмысленное «не удалось скачать».
        limits.check_disk_space(DOWNLOADS_DIR)
        # info уже получен, когда показывали кнопки качества — передаём его, чтобы
        # yt-dlp не ходил к площадке за теми же метаданными второй раз (экономит ~1.5с).
        file_path = await asyncio.to_thread(
            download_video, url, quality, on_progress, on_postprocess, info
        )
        # Не удаляем статус, а показываем «Отправляю» — заливка тоже занимает время
        await _safe_edit(progress_msg, t("uploading", lang))
        sent = await bot.send_video(
            chat_id,
            tg_files.input_file(file_path, await _nice_name(file_path, quality=f"{quality}p" if quality else None)),
            reply_to_message_id=user_msg_id,
            **await _video_kwargs(file_path, duration),
        )
        await progress_msg.delete()
        # Сохраняем file_id в кэш (сам файл убирается в finally — в том числе когда
        # отправка упала на полпути: раньше он оставался на диске до перезапуска).
        if sent.video:
            async with SessionLocal() as session:
                await save_cached_file_id(session, url, sent.video.file_id, str(quality))
    except Exception as e:
        logger.exception("Download failed")
        text = limits.friendly_error(e, lang)
        try:
            await progress_msg.edit_text(text)
        except Exception:
            # Полоски может не быть вовсе (упало на её отправке) — тогда отдельным
            # сообщением; не вышло и это — пусть падает в лог, слот уже освободится.
            try:
                await bot.send_message(chat_id, text)
            except Exception:
                logger.warning("Не смог сообщить о сбое загрузки в чат %s", chat_id)
    finally:
        _cleanup_all(file_path)
        await limits.release(limits.HEAVY, slot)
        ACTIVE_DOWNLOADS.discard(user_id)


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


def _cleanup_all(*paths):
    """Убирает всё, что скачали: принимает пути, списки путей и None.

    Зачем отдельно от _cleanup: уборка должна стоять в finally, а туда переменная
    попадает в любом состоянии — ещё не заведённая (скачивание упало на первой
    строке), одиночный путь или список файлов поста.
    """
    for item in paths:
        if item is None:
            continue
        if isinstance(item, (list, tuple, set)):
            _cleanup_all(*item)
        else:
            _cleanup(item)


def _cleanup(file_path: str):
    """Удаляет локальный файл после отправки — диск не копит мусор.
    Заодно учитываем размер в статистике трафика (сколько записано на SSD)."""
    try:
        if file_path and os.path.exists(file_path):
            traffic.record(file_path)
            os.remove(file_path)
    except Exception:
        logger.warning(f"Не смог удалить {file_path}")


@router.callback_query(F.data.startswith("sppage:"))
async def handle_collection_page(callback: CallbackQuery):
    """Переключение страниц списка треков"""
    lang = lang_of(callback.from_user)
    _, coll_id, page_str = callback.data.split(":")
    coll = COLLECTION_STORE.get(coll_id)
    if not coll:
        await callback.answer(t("link_expired", lang), show_alert=True)
        return
    keyboard = build_tracklist_keyboard(coll_id, coll["tracks"], int(page_str), coll.get("premium", False), lang)
    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("sptrk:"))
async def handle_collection_track(callback: CallbackQuery, bot: Bot):
    """Скачивание одного трека из альбома/плейлиста/сета"""
    lang = lang_of(callback.from_user)
    _, coll_id, idx_str = callback.data.split(":")
    coll = COLLECTION_STORE.get(coll_id)
    if not coll:
        await callback.answer(t("link_expired", lang), show_alert=True)
        return

    track = coll["tracks"][int(idx_str)]
    alerts.current_request.set(f"трек из коллекции: {track['cache_url']}")  # контекст для тревог
    await callback.answer()

    await _download_and_send_audio(
        bot, callback.message.chat.id, callback.from_user.id, callback.message.message_id,
        track["cache_url"], track["source"], lang,
        meta=track["meta"], fallback_query=track["fallback_query"],
    )


@router.callback_query(F.data.startswith("dlall:"))
async def handle_download_all(callback: CallbackQuery, bot: Bot):
    """Premium: скачивание всех треков альбома/плейлиста по очереди"""
    lang = lang_of(callback.from_user)
    _, coll_id = callback.data.split(":")
    coll = COLLECTION_STORE.get(coll_id)
    if not coll:
        await callback.answer(t("link_expired", lang), show_alert=True)
        return

    user_id = callback.from_user.id
    async with SessionLocal() as session:
        if not await is_premium(session, user_id):
            await callback.answer(t("premium_alert", lang), show_alert=True)
            return

    if user_id in ACTIVE_DOWNLOADS:
        await callback.answer(t("wait_current", lang), show_alert=True)
        return

    await callback.answer()
    chat_id = callback.message.chat.id
    reply_to = coll.get("user_msg_id")
    tracks = coll["tracks"]
    total = len(tracks)

    ACTIVE_DOWNLOADS.add(user_id)
    # Отправка первого сообщения — внутри try: если она не пройдёт, отметка «этот
    # человек уже качает» останется навсегда, и он до перезапуска будет получать
    # «дождись текущей загрузки». Тот же класс ошибки, что и с залипшим слотом.
    status = None
    try:
        status = await bot.send_message(
            chat_id, t("downloading_all", lang, i=0, total=total),
            reply_to_message_id=reply_to
        )
        for i, track in enumerate(tracks, 1):
            await _safe_edit(status, f"{t('downloading_all', lang, i=i, total=total)}\n{track['title']}")
            alerts.current_request.set(f"трек из коллекции [все]: {track['cache_url']}")
            try:
                await _do_download_audio(
                    bot, chat_id, reply_to,
                    track["cache_url"], track["source"], lang,
                    meta=track["meta"], fallback_query=track["fallback_query"],
                )
            except Exception:
                logger.exception("Track in 'download all' failed: %s", track["title"])
        await _safe_delete(status)
    finally:
        ACTIVE_DOWNLOADS.discard(user_id)
