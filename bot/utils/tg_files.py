"""
Как отдавать локальный файл Telegram и под каким именем.

Локальный Bot API-сервер умеет читать файл прямо с диска, если получил ссылку вида
«file:///путь». Обычная отправка вместо этого перекладывает файл в сервер по сети –
то есть уже скачанное видео путешествует второй раз. Замер: файл на 9.7 МБ уходит
за 0.08с по пути против 0.82с заливкой, и разрыв растёт вместе с размером.

Работает при двух условиях, и оба проверяются перед использованием:
  1) сервер запущен в локальном режиме (флаг --local, переменная TELEGRAM_LOCAL);
  2) файл лежит в папке, которую сервер видит (общий том с ботом).
Если хоть одно не выполнено – отдаём обычный FSInputFile, поведение прежнее.

ИМЯ ФАЙЛА. Скачивается всё под уникальными техническими именами (иначе параллельные
загрузки затирают друг друга), а человеку должно прийти «Название [1080p] @Бот.mp4».
Задать имя параметром нельзя: при отправке по «file://» Telegram берёт его из ПУТИ.
Поэтому перед отправкой делаем жёсткую ссылку с красивым именем и отдаём её. Ссылка,
а не переименование: вызывающий код потом удаляет исходный путь, и он должен остаться
на месте. Сами ссылки подчищаются здесь же по возрасту.
"""
import logging
import os
import re
import shutil
import time
import uuid
from urllib.parse import quote

from aiogram.types import FSInputFile

from bot.config import DOWNLOADS_DIR, TELEGRAM_LOCAL_API_URL, TELEGRAM_LOCAL

logger = logging.getLogger(__name__)

_SHARED_ROOT = os.path.abspath(DOWNLOADS_DIR)
# Подпапка для ссылок с красивыми именами. Отдельная, чтобы чистка не путала их с
# самими загрузками и чтобы имена не сталкивались между запросами.
NAMED_DIR = os.path.join(DOWNLOADS_DIR, "named")

# Сколько живёт ссылка после создания. Отправка занимает секунды, но на большом файле
# и медленном канале может тянуться, поэтому запас щедрый.
_LINK_TTL = 900
_links: list[tuple[float, str]] = []

# Символы, которых не должно быть в имени файла: часть запрещена файловыми системами,
# часть ломает разбор пути.
_BAD_CHARS = re.compile("[" + re.escape(r'\/:*?"<>|') + "]")
# Переводы строк и прочие управляющие символы заменяем ПРОБЕЛОМ, а не вырезаем:
# в названии с переносом вырезание склеивало соседние слова в одно.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f]")
# Длина имени ограничена файловой системой (255 БАЙТ, а не символов — кириллица
# занимает по два). Берём с запасом под « [1080p] @UnitySystemBot.mp4» и расширение.
_MAX_TITLE_BYTES = 120


def safe_name(title: str) -> str:
    """Приводит название к виду, пригодному для имени файла."""
    name = _CONTROL_CHARS.sub(" ", title or "")
    name = _BAD_CHARS.sub("", name).strip()
    # Точки в конце Windows молча отрезает, пробелы — тоже; убираем сами.
    name = name.rstrip(". ")
    name = re.sub(r"\s+", " ", name)
    while len(name.encode("utf-8")) > _MAX_TITLE_BYTES:
        name = name[:-1].rstrip()
    return name


def display_name(title: str, suffix: str, ext: str, bot_username: str = "UnitySystemBot") -> str:
    """Собирает имя вида «Название [1080p] @UnitySystemBot.mp4».

    suffix — то, что идёт в скобках (качество). Пустой suffix скобки не добавляет:
    у фото и гифок качества нет, а пустые «[]» выглядели бы поломкой.
    """
    title = safe_name(title)
    if not title:
        title = bot_username          # названия нет — пусть будет хоть что-то осмысленное
    suffix = (suffix or "").strip()
    # Одни вызывающие дают «1080p», другие — просто число (у HDRezka качество хранится
    # числом). Приводим к одному виду, чтобы имена не были разношёрстными.
    if suffix.isdigit():
        suffix = f"{suffix}p"
    tag = f" [{suffix}]" if suffix else ""
    ext = ext if ext.startswith(".") else f".{ext}"
    return f"{title}{tag} @{bot_username}{ext}"


def _sweep() -> None:
    """Убирает ссылки, которые уже точно не нужны."""
    now = time.time()
    keep = []
    for created, path in _links:
        if now - created < _LINK_TTL:
            keep.append((created, path))
            continue
        try:
            os.remove(path)
            os.rmdir(os.path.dirname(path))
        except OSError:
            pass
    _links[:] = keep


def _named_copy(path: str, name: str) -> str:
    """Путь с красивым именем, указывающий на те же данные.

    Жёсткая ссылка не копирует байты, поэтому на фильме в гигабайт это мгновенно. Если
    файловая система ссылок не поддерживает (бывает на смонтированных томах), честно
    копируем — медленнее, но работает.
    """
    _sweep()
    folder = os.path.join(NAMED_DIR, uuid.uuid4().hex[:8])
    os.makedirs(folder, exist_ok=True)
    link = os.path.join(folder, name)
    try:
        os.link(path, link)
    except OSError:
        shutil.copyfile(path, link)
    _links.append((time.time(), link))
    return link


def _is_shared(path: str) -> bool:
    """Лежит ли файл в общей с сервером папке."""
    try:
        return os.path.commonpath([os.path.abspath(path), _SHARED_ROOT]) == _SHARED_ROOT
    except ValueError:          # разные диски (Windows) – общего пути нет
        return False


def _file_url(path: str) -> str:
    """Путь → ссылка «file://» с экранированием.

    Экранирование обязательно: в имени теперь есть пробелы, скобки и кириллица, а в
    ссылке они недопустимы — сервер получил бы обрезанный путь и не нашёл файл. Косые
    черты оставляем как есть, иначе развалится сам путь.
    """
    return "file://" + quote(os.path.abspath(path).replace("\\", "/"), safe="/:")


def input_file(path: str, name: str | None = None):
    """FSInputFile или «file://»-ссылка, если сервер может прочитать файл сам.

    name — имя, под которым файл придёт человеку (см. display_name). Без него имя
    останется техническим, как было раньше.
    """
    if name:
        try:
            path = _named_copy(path, name)
        except OSError:
            logger.warning("Не смог дать файлу имя «%s» — отправлю как есть", name,
                           exc_info=True)
    if TELEGRAM_LOCAL and TELEGRAM_LOCAL_API_URL and _is_shared(path):
        return _file_url(path)
    return FSInputFile(path, filename=name) if name else FSInputFile(path)
