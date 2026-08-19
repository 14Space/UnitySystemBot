import logging
import os
import re
import requests
import yt_dlp
from urllib.parse import urlparse
from bot.features.download.downloaders.ytdlp_wrapper import BASE_OPTS, DOWNLOADS_DIR

try:
    from bot.config import INSTAGRAM_COOKIES, INSTAGRAM_PROXY
except Exception:  # worker может запускаться отдельно от бота
    INSTAGRAM_COOKIES = os.getenv("INSTAGRAM_COOKIES", "data/instagram_cookies.txt")
    INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "")

logger = logging.getLogger(__name__)

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXTS = (".mp4", ".mov", ".webm")

# Признаки, что Instagram отказал по анти-боту (а не «пост удалён»): при них есть смысл
# повторить запрос через прокси с другого IP.
_BLOCK_MARKERS = ("403", "429", "400", "rate limit", "checkpoint",
                  "login required", "empty media response", "temporarily")


def _looks_blocked(err: Exception) -> bool:
    s = str(err).lower()
    return any(m in s for m in _BLOCK_MARKERS)


def _proxy_attempts() -> list[str]:
    """Сначала прямое соединение (''), затем — прокси, если он задан. На домашнем IP
    хватает прямого; прокси включается запасным путём для дата-центрового IP (VPS)."""
    return ["", INSTAGRAM_PROXY] if INSTAGRAM_PROXY else [""]


def _cookies_path() -> str | None:
    """Путь к файлу кук, если он задан и существует.
    Куки залогиненного аккаунта дают доступ к контенту «не для всех»."""
    return INSTAGRAM_COOKIES if INSTAGRAM_COOKIES and os.path.exists(INSTAGRAM_COOKIES) else None


def _shortcode(url: str) -> str | None:
    """Достаёт код поста из ссылки: /p/CODE/, /reel/CODE/, /tv/CODE/"""
    m = re.search(r"/(?:p|reel|reels|tv)/([^/?#]+)", urlparse(url).path)
    return m.group(1) if m else None


def download_reel(url: str) -> str:
    """Скачивает Instagram Reel (видео) в максимальном качестве — как Shorts."""
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    output_path = os.path.join(DOWNLOADS_DIR, "%(id)s_viaSaver.%(ext)s")

    fmt = "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"

    base_opts = {
        **BASE_OPTS,
        "format": fmt,
        "outtmpl": output_path,
        "merge_output_format": "mp4",
    }
    # Куки залогиненного аккаунта — чтобы качать Reels с пометкой «доступ не для всех»
    cookies = _cookies_path()
    if cookies:
        base_opts["cookiefile"] = cookies

    last_err: Exception | None = None
    for proxy in _proxy_attempts():
        ydl_opts = dict(base_opts)
        if proxy:
            ydl_opts["proxy"] = proxy
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                extracted = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(extracted)
                if not os.path.exists(filename):
                    filename = filename.rsplit(".", 1)[0] + ".mp4"
                return filename
        except Exception as e:
            last_err = e
            # Прямой доступ заблокирован анти-ботом, а прокси ещё не пробовали — повторим.
            if not proxy and INSTAGRAM_PROXY and _looks_blocked(e):
                logger.info("Instagram: прямой доступ заблокирован (%s) — пробуем через прокси",
                            str(e)[:60])
                continue
            # «Доступ не для всех» — почти всегда вопрос кук. Подсказываем в лог, что делать.
            if "audiences" in str(e).lower() or "available to everyone" in str(e).lower():
                if not cookies:
                    logger.warning("Instagram: контент только для вошедших, а cookies.txt нет — "
                                   "добавь файл кук (INSTAGRAM_COOKIES), см. data/instagram_cookies.txt")
                else:
                    logger.warning("Instagram: контент только для вошедших, но даже с куками отказ — "
                                   "скорее всего сессия протухла, перевыгрузи cookies.txt")
            raise
    raise last_err


class PostUnavailable(Exception):
    """Пост не отдаётся: приватный, удалён или недоступен этому аккаунту."""


def _best_media(entry: dict) -> tuple[str, bool] | None:
    """Из элемента поста достаёт (ссылка_на_медиа, это_видео).
    Видео — лучший из formats; фото — самый крупный thumbnail. None, если пусто."""
    formats = entry.get("formats") or []
    if formats:
        # Берём формат с наибольшим разрешением (у Instagram они уже с прямыми ссылками)
        best = max(formats, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        if best.get("url"):
            return best["url"], True
    thumbs = entry.get("thumbnails") or []
    if thumbs:
        best = max(thumbs, key=lambda t: (t.get("height") or 0, t.get("width") or 0))
        if best.get("url"):
            return best["url"], False
    return None


def download_post(url: str) -> list[str]:
    """
    Скачивает пост Instagram целиком: одно фото/видео или всю карусель.
    Через yt-dlp: instaloader ходил в graphql, который Instagram закрыл (403/400),
    и посты перестали качаться совсем. Возвращает список файлов по порядку.
    """
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    shortcode = _shortcode(url) or "ig"

    base_opts = {
        **BASE_OPTS,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        # Фото-элементы не имеют видео-форматов: без этого yt-dlp роняет весь пост
        "ignoreerrors": True,
    }
    cookies = _cookies_path()
    if cookies:
        base_opts["cookiefile"] = cookies

    # Прямой доступ, при анти-бот отказе — повтор через прокси (см. _proxy_attempts).
    info, used_proxy, last_err = None, "", None
    for proxy in _proxy_attempts():
        opts = dict(base_opts)
        if proxy:
            opts["proxy"] = proxy
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                # process=False — не даём yt-dlp падать на фото («No video formats found»);
                # ссылки на медиа уже есть в самих элементах.
                info = ydl.extract_info(url, download=False, process=False)
        except Exception as e:
            last_err, info = e, None
        if info:
            used_proxy = proxy
            break
        if not proxy and INSTAGRAM_PROXY:
            logger.info("Instagram: пост не отдался напрямую — пробуем через прокси")

    if not info:
        raise PostUnavailable(f"Instagram не отдал пост {shortcode}"
                              + (f": {last_err}" if last_err else ""))

    # Медиа-файлы качаем тем же путём (прямо или через прокси), что и метаданные.
    proxies = {"http": used_proxy, "https": used_proxy} if used_proxy else None

    # Карусель приходит плейлистом, одиночный пост — обычным элементом
    entries = list(info.get("entries") or [info])

    files = []
    for i, entry in enumerate(entries, 1):
        if not entry:
            continue
        media = _best_media(entry)
        if not media:
            continue
        media_url, is_vid = media
        ext = ".mp4" if is_vid else ".jpg"
        path = os.path.join(DOWNLOADS_DIR, f"{shortcode}_{i}_viaSaver{ext}")
        data = requests.get(media_url, timeout=60, proxies=proxies).content
        with open(path, "wb") as f:
            f.write(data)
        files.append(path)

    return files


def is_image(path: str) -> bool:
    return path.lower().endswith(IMAGE_EXTS)


def is_video(path: str) -> bool:
    return path.lower().endswith(VIDEO_EXTS)
