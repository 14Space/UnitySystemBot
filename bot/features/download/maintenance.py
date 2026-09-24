import os
import sys
import glob
import shutil
import logging
import subprocess
import importlib.metadata as meta

from bot.config import DOWNLOADS_DIR, COOKIE_COPIES_DIR
from bot.utils import cookie_files

logger = logging.getLogger(__name__)


def clean_downloads():
    """Удаляет «хвосты» прошлых сессий из папки загрузок (вызывать ТОЛЬКО при старте,
    когда ничего не качается — иначе можно снести файл активной загрузки)."""
    # Одноразовые копии кук прошлого запуска – в своей папке, её уборка отдельно.
    copies = cookie_files.sweep(COOKIE_COPIES_DIR)
    if copies:
        logger.info("Убрано старых копий кук: %d", copies)
    if not os.path.isdir(DOWNLOADS_DIR):
        return
    removed = 0
    for path in glob.glob(os.path.join(DOWNLOADS_DIR, "*")):
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
            elif os.path.isdir(path):
                # Папка ссылок с «человеческими» именами (см. bot/utils/tg_files.py).
                # Раньше чистились только файлы, и такие папки копились бы навсегда.
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info("Очищено хвостов загрузок: %d", removed)


def update_ytdlp() -> tuple[str, str]:
    """Обновляет yt-dlp до последней НОЧНОЙ сборки (сайты часто меняются, а скачивание
    через SABR есть только в nightly). Обычная стабильная версия сюда не годится —
    она откатит SABR и вернёт 403 на YouTube.

    Возвращает (было, стало). Если версии совпали — обновлять было нечего. Новая версия
    начинает работать только после перезапуска процесса, поэтому вызывающий код решает,
    перезапускаться ли (см. _daily_tasks).
    """
    try:
        old = meta.version("yt-dlp")
    except Exception:
        old = "?"
    # Ночные сборки лежат на обычном PyPI как пре-релизы – сторонний индекс не нужен
    # (раньше был --extra-index-url: лишний индекс = лишний путь подсунуть пакет).
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "--quiet", "--pre", "yt-dlp[default]"],
            check=True, timeout=300,
        )
        new = meta.version("yt-dlp")
        if new != old:
            logger.info("yt-dlp обновлён: %s -> %s (применится после перезапуска)", old, new)
        else:
            logger.info("yt-dlp уже последней версии (%s)", old)
        return old, new
    except Exception as e:
        logger.warning("Не удалось обновить yt-dlp: %s", e)
    return old, old

