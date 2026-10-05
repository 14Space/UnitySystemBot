"""
Точка входа для ссылок: вынимает ссылку из сообщения, узнаёт площадку и передаёт её
обработчику этой площадки (bot/features/download/flows/*).

Раньше всё это было одним файлом на 1600 строк. Здесь осталось только то, что общее
для всех площадок, – путь сообщения до нужного обработчика.
"""
import asyncio
import logging
from functools import partial
from urllib.parse import urlparse
from aiogram import Router, F
from aiogram.types import Message
from bot.utils.platform_detector import detect_platform, Platform, extract_url
from bot.utils import net
from bot.features.common import alerts
from bot.utils.i18n import lang_of
from bot.database import SessionLocal
from bot.database.repository import increment_download
from bot.features.download.downloaders.ytdlp_wrapper import (
    youtube_search_query, download_shorts, resolve_short,
)
from bot.features.download.downloaders.instagram import download_reel, download_post
from bot.features.download.flows.common import _shorts_cap, _shorts_key
from bot.features.download.flows.hdrezka import _handle_hdrezka
from bot.features.download.flows.music import (
    _handle_audio,
    _handle_soundcloud_set,
    _handle_spotify,
    _handle_spotify_collection,
    _soundcloud_query,
)
from bot.features.download.flows.tiktok import _handle_tiktok
from bot.features.download.flows.twitter import _handle_twitter
from bot.features.download.flows.video import (
    _handle_files,
    _handle_media,
    _handle_quality_video,
    _handle_simple_video,
)
from bot.features.download.flows import (
    hdrezka as hdrezka_flow, music, tiktok as tiktok_flow, video,
)

router = Router()
# Кнопки площадок – в их модулях; сообщения со ссылками принимает этот роутер.
for _sub in (video.router, hdrezka_flow.router, tiktok_flow.router, music.router):
    router.include_router(_sub)
logger = logging.getLogger(__name__)


@router.message(F.text | F.caption)
async def handle_link(message: Message):
    # Берём из сообщения именно ССЫЛКУ, а не весь текст: рядом с ней почти всегда
    # идёт подпись, и раньше она уезжала на площадку как часть адреса.
    #
    # Смотрим и ПОДПИСЬ к медиа: ссылку часто присылают подписью к пересланному
    # посту. Раньше обработчик стоял только на F.text, и такие сообщения бот не
    # видел вовсе — выглядело как «на эту ссылку не реагирует».
    await process_link(message, extract_url(message.text or message.caption or ""))


async def process_link(message: Message, url: str):
    """Обработка одной ссылки. Вызывается из текстовых сообщений и из inline-перехода."""
    if not url.startswith("http"):
        return
    # Автора может не быть вовсе: анонимный админ группы, пост от имени канала. Без
    # него не узнать ни язык, ни премиум, а обращение к from_user.id роняло обработчик
    # уже ПОСЛЕ показа «Скачиваю…» — человек видел зависшее сообщение.
    if getattr(message, "from_user", None) is None:
        logger.info("Ссылка без автора (анонимный админ или канал) — пропускаю")
        return

    lang = lang_of(message.from_user)
    platform = detect_platform(url)

    # Неподдерживаемая ссылка — молчим (особенно важно в группах: не реагируем
    # на чужие/сторонние ссылки, чтобы не спамить и не отвечать невпопад).
    if platform == Platform.UNKNOWN:
        return

    # Короткая ссылка SoundCloud (on.soundcloud.com) может вести и на трек, и на целый
    # сет – что это, видно только после разворота (см. resolve_short).
    if net.url_on(url, "on.soundcloud.com"):
        try:
            url = await asyncio.to_thread(resolve_short, url)
        except Exception:
            logger.info("Короткая ссылка SoundCloud не развернулась: %s", url)
        platform = detect_platform(url)

    # Контекст для уведомления админу о сбое (какая площадка и ссылка). Выставляем ДО
    # запуска обработки, чтобы дочерние задачи скачивания его унаследовали.
    alerts.current_request.set(f"{platform.value}: {url}")

    # Статистика популярности платформ
    async with SessionLocal() as session:
        await increment_download(session, platform.value)

    await _dispatch_platform(message, url, platform, lang)


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
        return  # ВРЕМЕННО ig_off: посты и карусели Instagram пока недоступны – молчим
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
