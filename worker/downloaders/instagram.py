import os
import re
import requests
import yt_dlp
import instaloader
from urllib.parse import urlparse
from worker.downloaders.ytdlp_wrapper import BASE_OPTS, DOWNLOADS_DIR

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXTS = (".mp4", ".mov", ".webm")

# Один загрузчик на процесс. Анонимно (без логина) — для публичных постов.
_loader = None


def _get_loader():
    global _loader
    if _loader is None:
        _loader = instaloader.Instaloader(
            download_comments=False, save_metadata=False, quiet=True
        )
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

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        extracted = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(extracted)
        if not os.path.exists(filename):
            filename = filename.rsplit(".", 1)[0] + ".mp4"
        return filename


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
