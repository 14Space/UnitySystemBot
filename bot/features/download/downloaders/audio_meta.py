import logging
from io import BytesIO
import requests
from PIL import Image
from mutagen.id3 import ID3, APIC, TIT2, TPE1
from mutagen.id3 import error as ID3Error
from mutagen.mp3 import MP3

logger = logging.getLogger(__name__)


def _fetch_resized(cover_url: str, max_size: int) -> bytes | None:
    """Скачивает обложку и ужимает до max_size px (квадрат вписывается), отдаёт JPEG."""
    if not cover_url:
        return None
    try:
        data = requests.get(cover_url, timeout=15).content
        img = Image.open(BytesIO(data)).convert("RGB")
        img.thumbnail((max_size, max_size))
        out = BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception:
        logger.warning("Не смог обработать обложку %s", cover_url, exc_info=True)
        return None


def make_thumbnail(cover_url: str) -> bytes | None:
    """Миниатюра для кружка Telegram (≤320px, JPEG)."""
    return _fetch_resized(cover_url, 320)


def set_metadata(mp3_path: str, cover_url: str = None, title: str = None, artist: str = None):
    """
    Перезаписывает теги mp3 точными данными из источника (Spotify/SoundCloud).
    Главное — вшивает ПРАВИЛЬНУЮ обложку вместо случайной с найденного YouTube-видео.
    """
    try:
        audio = MP3(mp3_path, ID3=ID3)
        try:
            audio.add_tags()
        except ID3Error:
            pass  # теги уже есть

        if title:
            audio.tags.setall("TIT2", [TIT2(encoding=3, text=title)])
        if artist:
            audio.tags.setall("TPE1", [TPE1(encoding=3, text=artist)])
        if cover_url:
            img = _fetch_resized(cover_url, 800)  # не вшиваем гигантские 3000px/11МБ
            if img:
                audio.tags.delall("APIC")  # убираем любую чужую обложку
                audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=img))

        audio.save()
    except Exception:
        logger.warning("Не смог вшить теги/обложку в %s", mp3_path, exc_info=True)


def get_soundcloud_cover(url: str) -> str | None:
    """Достаёт обложку трека SoundCloud через публичный oEmbed (без авторизации).
    Нужно для DRM-треков, которые мы качаем поиском с YouTube."""
    try:
        r = requests.get(
            "https://soundcloud.com/oembed",
            params={"url": url, "format": "json"},
            timeout=10,
        )
        thumb = r.json().get("thumbnail_url")
        # oEmbed отдаёт мелкое превью (...-t500x500.jpg или -large.jpg) — оставляем как есть
        return thumb
    except Exception:
        return None
