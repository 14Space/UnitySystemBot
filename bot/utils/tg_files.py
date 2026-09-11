"""
Как отдавать локальный файл Telegram.

Локальный Bot API-сервер умеет читать файл прямо с диска, если получил ссылку вида
«file:///путь». Обычная отправка вместо этого перекладывает файл в сервер по сети –
то есть уже скачанное видео путешествует второй раз. Замер: файл на 9.7 МБ уходит
за 0.08с по пути против 0.82с заливкой, и разрыв растёт вместе с размером.

Работает при двух условиях, и оба проверяются перед использованием:
  1) сервер запущен в локальном режиме (флаг --local, переменная TELEGRAM_LOCAL);
  2) файл лежит в папке, которую сервер видит (общий том с ботом).
Если хоть одно не выполнено – отдаём обычный FSInputFile, поведение прежнее.
"""
import os

from aiogram.types import FSInputFile

from bot.config import DOWNLOADS_DIR, TELEGRAM_LOCAL_API_URL, TELEGRAM_LOCAL

_SHARED_ROOT = os.path.abspath(DOWNLOADS_DIR)


def _is_shared(path: str) -> bool:
    """Лежит ли файл в общей с сервером папке."""
    try:
        return os.path.commonpath([os.path.abspath(path), _SHARED_ROOT]) == _SHARED_ROOT
    except ValueError:          # разные диски (Windows) – общего пути нет
        return False


def input_file(path: str):
    """FSInputFile или «file://»-ссылка, если сервер может прочитать файл сам."""
    if TELEGRAM_LOCAL and TELEGRAM_LOCAL_API_URL and _is_shared(path):
        return "file://" + os.path.abspath(path)
    return FSInputFile(path)
