"""
Расшифровка голосовых сообщений (voice) и видео-кружков (video_note).

Логика: в ответ на ГС/кружок бот сразу пишет «Расшифровываю...», скачивает файл,
прогоняет через локальный Whisper и редактирует то же сообщение на распознанный
текст. Если ничего не распознано – пишет «Не удалось ничего распознать...».
"""
import asyncio
import glob
import logging
import os
import shutil
import uuid

from aiogram import Router, F
from aiogram.types import Message

from bot.config import (
    DOWNLOADS_DIR, TELEGRAM_LOCAL_API_URL, TELEGRAM_LOCAL_FILES_DIR,
    TELEGRAM_BOT_API_ROOT, TELEGRAM_API_CONTAINER, BOT_TOKEN,
)
from bot.utils import limits
from bot.utils.i18n import t, lang_of
from worker.transcriber import transcribe

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

    await limits.acquire(limits.TRANSCRIBE)
    try:
        await _fetch_file(message, file_id, file_path)
        text = await asyncio.to_thread(transcribe, file_path)

        if not text:
            await _safe_edit(status, t("transcribe_nothing", lang))
            return

        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT - 1] + "…"
        await _safe_edit(status, text)
    except Exception:
        logger.exception("Transcription failed")
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
    """
    if TELEGRAM_LOCAL_API_URL:
        file = await message.bot.get_file(file_id)
        # 1) файл виден напрямую (бот и сервер делят том) или через bind-папку
        if file.file_path and os.path.exists(file.file_path):
            await asyncio.to_thread(shutil.copyfile, file.file_path, dest)
            return
        local = _map_local_path(file.file_path)
        if local and os.path.exists(local):
            await asyncio.to_thread(shutil.copyfile, local, dest)
            return
        # 2) забираем файл из контейнера сервера
        if await _docker_cp(file.file_path, dest):
            return
    await message.bot.download(file_id, destination=dest)


async def _docker_cp(server_path: str, dest: str) -> bool:
    """Копирует файл из контейнера Bot API на хост через `docker cp`. True при успехе."""
    if not server_path:
        return False
    sp = server_path.replace("\\", "/")
    # getFile отдаёт относительный путь (voice/file_0.oga) — дополняем папкой токена
    if sp.startswith("/"):
        container_path = sp
    else:
        container_path = f"{TELEGRAM_BOT_API_ROOT.rstrip('/')}/{BOT_TOKEN}/{sp}"
    # docker cp делит аргумент по ПЕРВОМУ двоеточию (контейнер:путь), двоеточие
    # внутри пути (в токене) остаётся частью пути — то, что нам нужно.
    proc = await asyncio.create_subprocess_exec(
        "docker", "cp", f"{TELEGRAM_API_CONTAINER}:{container_path}", dest,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
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


async def _safe_edit(msg, text: str):
    try:
        await msg.edit_text(text)
    except Exception:
        pass


def _cleanup(file_path: str):
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        logger.warning("Не смог удалить %s", file_path)
