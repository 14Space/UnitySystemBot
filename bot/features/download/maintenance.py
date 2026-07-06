import os
import sys
import glob
import logging
import subprocess
import importlib.metadata as meta

from bot.features.download.downloaders.ytdlp_wrapper import DOWNLOADS_DIR

logger = logging.getLogger(__name__)


def clean_downloads():
    """Удаляет «хвосты» прошлых сессий из папки загрузок (вызывать ТОЛЬКО при старте,
    когда ничего не качается — иначе можно снести файл активной загрузки)."""
    if not os.path.isdir(DOWNLOADS_DIR):
        return
    removed = 0
    for path in glob.glob(os.path.join(DOWNLOADS_DIR, "*")):
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info("Очищено хвостов загрузок: %d", removed)


def update_ytdlp():
    """Обновляет yt-dlp до последней версии (сайты часто меняются).
    Новая версия применяется после перезапуска бота."""
    try:
        old = meta.version("yt-dlp")
    except Exception:
        old = "?"
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "--quiet", "yt-dlp"],
            check=True, timeout=300,
        )
        new = meta.version("yt-dlp")
        if new != old:
            logger.info("yt-dlp обновлён: %s -> %s (применится после перезапуска)", old, new)
        else:
            logger.info("yt-dlp уже последней версии (%s)", old)
    except Exception as e:
        logger.warning("Не удалось обновить yt-dlp: %s", e)
