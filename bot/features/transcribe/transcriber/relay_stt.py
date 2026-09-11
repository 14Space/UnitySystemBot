"""
Последний рубеж расшифровки: чужой бот в Telegram через аккаунт-посредник.

Нужен потому, что бот НЕ МОЖЕТ написать боту — это запрет самого Telegram. Поэтому
письмо отправляет обычный пользовательский аккаунт (Telethon), а мы читаем ответ.

Почему именно последний: он самый медленный из трёх (замер 2.2с против 0.45с у Groq
и 1.12с на нашей видеокарте), требует круглосуточно живой пользовательской сессии и
гоняет чужие голосовые через личный профиль. Включается только когда оба первых
способа отказали.

Особенность этого бота: он сперва отвечает «Аудиосообщение принято!», а затем
РЕДАКТИРУЕТ это же сообщение, вписывая расшифровку. Поэтому ждём не новое сообщение,
а изменение уже пришедшего.
"""
import asyncio
import logging
import os
import re

from bot.config import (
    RELAY_STT_BOT, RELAY_STT_SESSION, RELAY_STT_TIMEOUT,
    TELEGRAM_API_ID, TELEGRAM_API_HASH,
)

logger = logging.getLogger(__name__)

# Подтверждение приёма, а не результат: увидев такое, продолжаем ждать правку.
_ACK = re.compile(r"принято|обрабат|получен|ожидай|подожд|секунд", re.I)

_client = None
_lock = asyncio.Lock()


def available() -> bool:
    return bool(RELAY_STT_SESSION and os.path.exists(RELAY_STT_SESSION)
                and TELEGRAM_API_ID and TELEGRAM_API_HASH)


async def _get_client():
    """Один общий клиент на весь процесс: каждое подключение — это вход в аккаунт,
    и частые входы Telegram воспринимает плохо."""
    global _client
    if _client is not None and _client.is_connected():
        return _client
    from telethon import TelegramClient
    # Без расширения: Telethon сам добавит .session
    name = RELAY_STT_SESSION[:-8] if RELAY_STT_SESSION.endswith(".session") else RELAY_STT_SESSION
    _client = TelegramClient(name, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    await _client.connect()
    if not await _client.is_user_authorized():
        await _client.disconnect()
        _client = None
        raise RuntimeError("сессия-посредник не авторизована")
    return _client


async def transcribe(file_path: str) -> str:
    """Шлёт файл чужому боту и ждёт расшифровку. Бросает исключение, если не вышло."""
    client = await _get_client()
    # Строго по одному: параллельные отправки от одного аккаунта выглядят как флуд.
    async with _lock:
        last = await client.get_messages(RELAY_STT_BOT, limit=1)
        last_id = last[0].id if last else 0
        await client.send_file(RELAY_STT_BOT, file_path, voice_note=True)

        reply_id = None
        deadline = asyncio.get_event_loop().time() + RELAY_STT_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.3)
            if reply_id is None:
                for m in reversed(await client.get_messages(RELAY_STT_BOT,
                                                            min_id=last_id, limit=20)):
                    text = (m.message or "").strip()
                    if m.out or not text:
                        continue
                    if not _ACK.search(text):
                        return text              # сразу прислал результат
                    reply_id = m.id              # это подтверждение, ждём правку
                    break
                continue
            msg = await client.get_messages(RELAY_STT_BOT, ids=reply_id)
            text = (getattr(msg, "message", "") or "").strip()
            if text and not _ACK.search(text):
                return text
    raise TimeoutError(f"бот-расшифровщик не ответил за {RELAY_STT_TIMEOUT}с")
