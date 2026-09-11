"""
Расшифровка голосовых сообщений (voice) и видео-кружков (video_note).

Логика: в ответ на ГС/кружок бот сразу пишет «Расшифровываю...», скачивает файл,
прогоняет через локальный Whisper и редактирует то же сообщение на распознанный
текст. Если ничего не распознано – пишет «Не удалось ничего распознать...».
"""
import asyncio
import glob
import html
import logging
import os
import shutil
import uuid

from aiogram import Router, F
from aiogram.types import Message

from bot.config import (
    DOWNLOADS_DIR, TELEGRAM_LOCAL_API_URL, TELEGRAM_LOCAL_FILES_DIR,
    TELEGRAM_BOT_API_ROOT, TELEGRAM_API_CONTAINER,
)
from bot.utils import limits, traffic
from bot.utils.i18n import t, lang_of
from bot.features.common import alerts
from bot.features.transcribe.transcriber import transcribe

router = Router()
logger = logging.getLogger(__name__)

# Telegram ограничивает caption/text 4096 символами – длинную расшифровку режем.
MAX_TEXT = 4096


async def _handle(message: Message, file_id: str, suffix: str):
    """Общий обработчик: качаем файл, расшифровываем, показываем результат."""
    lang = lang_of(message.from_user)
    status = await message.reply(t("transcribing", lang))

    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOADS_DIR, f"{uuid.uuid4().hex}{suffix}")

    alerts.current_request.set("расшифровка (голосовое/кружок)")  # контекст для тревог
    await limits.acquire(limits.TRANSCRIBE)
    try:
        await _fetch_file(message, file_id, file_path)
        text = await asyncio.to_thread(transcribe, file_path)

        # Пусто = тишина/музыка без слов (или остались одни титры-галлюцинации, которые
        # мы вырезали). В этом случае бот просто молчит — убираем «Расшифровываю…».
        if not text:
            try:
                await status.delete()
            except Exception:
                pass
            return

        # Оформляем расшифровку «цитатой» (как голосовое-первоисточник). Берём с
        # запасом под теги blockquote и экранирование спецсимволов HTML.
        if len(text) > MAX_TEXT - 100:
            text = text[:MAX_TEXT - 100] + "…"
        quoted = f"<blockquote expandable>{html.escape(text)}</blockquote>"
        await _safe_edit(status, quoted, parse_mode="HTML")
    except Exception as e:
        logger.exception("Transcription failed")
        alerts.note_failure(e)            # раньше сбои расшифровки молчали в алертах
        await _safe_edit(status, t("transcribe_nothing", lang))
    finally:
        await limits.release(limits.TRANSCRIBE)
        _cleanup(file_path)


async def _fetch_file(message: Message, file_id: str, dest: str):
    """
    Кладёт файл из Telegram в dest. Локальный Bot API сервер не отдаёт файлы по
    HTTP — он держит их на диске внутри своего Docker-контейнера. Поэтому:
      1) если бот сам в Docker и том примонтирован — читаем напрямую;
      2) если бот на хосте — копируем из контейнера через `docker cp`;
      3) иначе (облачный API) — обычное скачивание.

    В локальном режиме (--local) сервер НЕ удаляет принятые файлы сам: это забота бота.
    Поэтому свою копию мы забираем, а исходник у сервера подчищаем, иначе его папка
    растёт бесконечно.
    """
    if TELEGRAM_LOCAL_API_URL:
        # Токен бота, принявшего голосовое: локальный сервер хранит файлы под папкой
        # токена, и getFile отдаёт путь именно внутри неё.
        token = message.bot.token
        file = await message.bot.get_file(file_id)
        # 1) файл виден напрямую (бот и сервер делят том) или через bind-папку
        if file.file_path and os.path.exists(file.file_path):
            await asyncio.to_thread(shutil.copyfile, file.file_path, dest)
            _drop_server_copy(file.file_path)
            return
        # 1b) бот в Docker: том сервера примонтирован (TELEGRAM_BOT_API_ROOT), но
        # getFile отдаёт ОТНОСИТЕЛЬНЫЙ путь (voice/file_0.oga) — собираем абсолютный
        # путь внутри тома и читаем файл напрямую, без docker cp.
        container = _container_path(file.file_path, token)
        if container and os.path.exists(container):
            await asyncio.to_thread(shutil.copyfile, container, dest)
            _drop_server_copy(container)
            return
        local = _map_local_path(file.file_path)
        if local and os.path.exists(local):
            await asyncio.to_thread(shutil.copyfile, local, dest)
            _drop_server_copy(local)
            return
        # 2) забираем файл из контейнера сервера
        if await _docker_cp(file.file_path, dest, token):
            return
    await message.bot.download(file_id, destination=dest)


def _drop_server_copy(path: str):
    """Удаляет файл из папки Bot API-сервера после того, как забрали свою копию.
    В локальном режиме сервер этого не делает, а мы файл больше не используем.
    Если папка примонтирована только на чтение — молча пропускаем, это не ошибка."""
    try:
        os.remove(path)
    except OSError:
        pass


def _container_path(server_path: str, token: str) -> str | None:
    """Абсолютный путь файла внутри тома Bot API (бот в Docker читает его напрямую).
    getFile отдаёт относительный путь (voice/file_0.oga) — дополняем корнем тома
    и папкой токена ЭТОГО бота. Абсолютный путь от сервера берём как есть."""
    if not server_path:
        return None
    sp = server_path.replace("\\", "/")
    if sp.startswith("/"):
        return sp
    return f"{TELEGRAM_BOT_API_ROOT.rstrip('/')}/{token}/{sp}"


async def _docker_cp(server_path: str, dest: str, token: str) -> bool:
    """Копирует файл из контейнера Bot API на хост через `docker cp`. True при успехе."""
    container_path = _container_path(server_path, token)
    if not container_path:
        return False
    # docker cp делит аргумент по ПЕРВОМУ двоеточию (контейнер:путь), двоеточие
    # внутри пути (в токене) остаётся частью пути — то, что нам нужно.
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "cp", f"{TELEGRAM_API_CONTAINER}:{container_path}", dest,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        # docker не установлен (например, бот сам в контейнере) — не наш путь
        return False
    _, err = await proc.communicate()
    if proc.returncode == 0 and os.path.exists(dest):
        return True
    logger.warning("docker cp не сработал: %s", (err or b"").decode(errors="ignore").strip())
    return False


def _map_local_path(server_path: str) -> str | None:
    """
    Переводит путь файла из Bot API в путь на хосте.

    Файл лежит в TELEGRAM_LOCAL_FILES_DIR/<папка-токена>/voice|video_notes/file_N.
    Имя папки токена содержит двоеточие, которое Docker/WSL на NTFS подменяет
    спецсимволом Unicode — поэтому собрать путь напрямую нельзя. Берём последние
    два элемента пути (voice/file_0.oga) и ищём через glob с «*» вместо токена.
    """
    if not server_path:
        return None
    parts = server_path.replace("\\", "/").strip("/").split("/")
    tail = parts[-2:] if len(parts) >= 2 else parts
    matches = glob.glob(os.path.join(TELEGRAM_LOCAL_FILES_DIR, "*", *tail))
    return matches[0] if matches else None


@router.message(F.voice)
async def handle_voice(message: Message):
    await _handle(message, message.voice.file_id, ".ogg")


@router.message(F.video_note)
async def handle_video_note(message: Message):
    await _handle(message, message.video_note.file_id, ".mp4")


async def _safe_edit(msg, text: str, parse_mode: str | None = None):
    try:
        await msg.edit_text(text, parse_mode=parse_mode)
    except Exception:
        pass


def _cleanup(file_path: str):
    try:
        if file_path and os.path.exists(file_path):
            traffic.record(file_path)
            os.remove(file_path)
    except Exception:
        logger.warning("Не смог удалить %s", file_path)
