"""
Извлечение аудиодорожки к видео «лёгких» платформ (для тумблера в /setconfig).
TikTok — родной оригинальный звук из API (работает и для слайдшоу), остальные —
через yt-dlp прямо из видео. Возвращает (путь_к_mp3, заголовок|None) или None.
"""
import logging
import os

from bot.features.download.downloaders import tiktok
from bot.features.download.downloaders.ytdlp_wrapper import download_audio
from bot.utils.platform_detector import Platform

logger = logging.getLogger(__name__)


def extract_audio_track(url: str, platform: Platform) -> tuple[str, str | None] | None:
    try:
        if platform == Platform.TIKTOK:
            return tiktok.download_music(url)
        path = download_audio(url, embed_thumbnail=False)
        if path and os.path.exists(path) and os.path.getsize(path) > 0:
            return path, None       # заголовок возьмётся из тегов mp3
        return None
    except Exception:
        logger.info("Не удалось извлечь аудиодорожку для %s", url)
        return None
