import http.cookiejar
import logging
import os
import re
import requests
import yt_dlp
import instaloader
from urllib.parse import urlparse
from worker.downloaders.ytdlp_wrapper import BASE_OPTS, DOWNLOADS_DIR

try:
    from bot.config import INSTAGRAM_COOKIES
except Exception:  # worker может запускаться отдельно от бота
    INSTAGRAM_COOKIES = os.getenv("INSTAGRAM_COOKIES", "data/instagram_cookies.txt")

logger = logging.getLogger(__name__)

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXTS = (".mp4", ".mov", ".webm")

# Один загрузчик на процесс. Если есть cookies.txt залогиненного аккаунта — подгружаем
# их (доступ к контенту «не для всех»), иначе ходим анонимно (только публичное).
_loader = None


def _cookies_path() -> str | None:
    """Путь к файлу кук, если он задан и существует."""
    return INSTAGRAM_COOKIES if INSTAGRAM_COOKIES and os.path.exists(INSTAGRAM_COOKIES) else None


def _get_loader():
    global _loader
    if _loader is None:
        _loader = instaloader.Instaloader(
            download_comments=False, save_metadata=False, quiet=True
        )
        path = _cookies_path()
        if path:
            try:
                # instaloader работает через requests.Session — вливаем в неё куки из файла
                jar = http.cookiejar.MozillaCookieJar(path)
                jar.load(ignore_discard=True, ignore_expires=True)
                _loader.context._session.cookies.update(jar)
                logger.info("Instagram: куки залогиненного аккаунта загружены")
            except Exception:
                logger.warning("Instagram: не удалось загрузить cookies.txt", exc_info=True)
    return _loader


def _shortcode(url: str) -> str | None:
    """Достаёт код поста из ссылки: /p/CODE/, /reel/CODE/, /tv/CODE/"""
    m = re.search(r"/(?:p|reel|reels|tv)/([^/?#]+)", urlparse(url).path)
    return m.group(1) if m else None


def download_reel(url: str) -> str:
    """Скачивает Instagram Reel (видео) в максимальном качестве — как Shorts."""
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    output_path = os.path.join(DOWNLOADS_DIR, "%(id)s_viaSaver.%(ext)s")

    fmt = "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"

    ydl_opts = {
        **BASE_OPTS,
        "format": fmt,
        "outtmpl": output_path,
        "merge_output_format": "mp4",
    }
    # Куки залогиненного аккаунта — чтобы качать Reels с пометкой «доступ не для всех»
    cookies = _cookies_path()
    if cookies:
        ydl_opts["cookiefile"] = cookies

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            extracted = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(extracted)
            if not os.path.exists(filename):
                filename = filename.rsplit(".", 1)[0] + ".mp4"
            return filename
    except Exception as e:
        # «Доступ не для всех» — почти всегда вопрос кук. Подсказываем в лог, что делать.
        if "audiences" in str(e).lower() or "available to everyone" in str(e).lower():
            if not cookies:
                logger.warning("Instagram: контент только для вошедших, а cookies.txt нет — "
                               "добавь файл кук (INSTAGRAM_COOKIES), см. data/instagram_cookies.txt")
            else:
                logger.warning("Instagram: контент только для вошедших, но даже с куками отказ — "
                               "скорее всего сессия протухла, перевыгрузи cookies.txt")
        raise


def download_post(url: str) -> list[str]:
    """
    Скачивает пост Instagram целиком: одно фото/видео или всю карусель.
    Использует instaloader (yt-dlp умеет только видео). Возвращает список файлов по порядку.
    """
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    shortcode = _shortcode(url)
    if not shortcode:
        return []

    loader = _get_loader()
    post = instaloader.Post.from_shortcode(loader.context, shortcode)

    # Собираем список медиа: (ссылка, это_видео)
    media = []
    if post.typename == "GraphSidecar":  # карусель
        for node in post.get_sidecar_nodes():
            media.append((node.video_url if node.is_video else node.display_url, node.is_video))
    elif post.is_video:
        media.append((post.video_url, True))
    else:
        media.append((post.url, False))

    files = []
    for i, (media_url, is_vid) in enumerate(media, 1):
        ext = ".mp4" if is_vid else ".jpg"
        path = os.path.join(DOWNLOADS_DIR, f"{shortcode}_{i}_viaSaver{ext}")
        data = requests.get(media_url, timeout=60).content
        with open(path, "wb") as f:
            f.write(data)
        files.append(path)

    return files


def is_image(path: str) -> bool:
    return path.lower().endswith(IMAGE_EXTS)


def is_video(path: str) -> bool:
    return path.lower().endswith(VIDEO_EXTS)
