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

Сколько отправок идёт разом — задаёт RELAY_STT_CONCURRENCY (0 = без ограничения).
Одновременность упирается не в лимиты бота, а в аккаунт-посредник и в то, что при
нескольких отправках в работе ответы надо различать между собой — см. _await_answer.
"""
import asyncio
import logging
import os
import re

from bot.config import (
    RELAY_STT_BOT, RELAY_STT_SESSION, RELAY_STT_TIMEOUT,
    RELAY_STT_CONCURRENCY, RELAY_STT_MIN_GAP,
    TELEGRAM_API_ID, TELEGRAM_API_HASH,
)

logger = logging.getLogger(__name__)

# Подтверждение приёма, а не результат: увидев такое, продолжаем ждать правку.
_ACK = re.compile(r"принято|обрабат|получен|ожидай|подожд|секунд", re.I)

_client = None
# Сколько отправок идёт разом. Ограничиваем не бота, а аккаунт-посредник: Telegram
# не любит автоматические отправки с живых аккаунтов. Величину задаёт настройка,
# 0 = без ограничения. Раньше здесь был Lock, то есть жёсткое «по одному», и очередь
# из голосовых обрабатывалась строго последовательно.
_gate = asyncio.Semaphore(RELAY_STT_CONCURRENCY) if RELAY_STT_CONCURRENCY > 0 else None
# Момент последней отправки — чтобы держать зазор даже когда мест в семафоре хватает.
_sent_lock = asyncio.Lock()
_last_sent = 0.0
# Сколько наших отправок сейчас ждут ответа: по нему решаем, можно ли
# доверять позиционному поиску ответа (см. _await_answer).
_inflight = 0


class _NoGate:
    """Заглушка под «без ограничения»: async with, который ничего не делает."""

    async def __aenter__(self): return self

    async def __aexit__(self, *exc): return False


async def _space_out() -> None:
    """Выдерживает RELAY_STT_MIN_GAP между отправками. Считаем по монотонным часам:
    перевод системного времени не должен приводить ни к залипанию, ни к залпу."""
    global _last_sent
    if RELAY_STT_MIN_GAP <= 0:
        return
    async with _sent_lock:
        wait = _last_sent + RELAY_STT_MIN_GAP - asyncio.get_event_loop().time()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_sent = asyncio.get_event_loop().time()


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


async def authorized() -> bool:
    """Жива ли сессия-посредник на самом деле (не «лежит ли файл», а признаёт ли её
    Telegram). Отдельно от available(), потому что файл сессии переживает её отзыв:
    завершил сеансы в настройках Telegram — файл на месте, входа больше нет."""
    if not available():
        return False
    try:
        await _get_client()
    except Exception:
        return False
    return True


async def transcribe(file_path: str) -> str:
    """Шлёт файл чужому боту и ждёт расшифровку. Бросает исключение, если не вышло."""
    global _inflight
    client = await _get_client()
    async with (_gate or _NoGate()):
        await _space_out()
        last = await client.get_messages(RELAY_STT_BOT, limit=1)
        last_id = last[0].id if last else 0
        sent = await client.send_file(RELAY_STT_BOT, file_path, voice_note=True)
        my_id = getattr(sent, "id", 0)
        _inflight += 1
        try:
            return await _await_answer(client, last_id, my_id)
        finally:
            _inflight -= 1


def _answers_me(msg, my_id: int) -> bool:
    """Ответил ли бот именно на НАШУ отправку (а не на соседнюю, идущую параллельно)."""
    return bool(my_id) and getattr(msg, "reply_to_msg_id", None) == my_id


async def _await_answer(client, last_id: int, my_id: int) -> str:
    """Ждёт расшифровку своей отправки.

    Пока отправки шли строго по одной, «ответ бота» можно было брать позиционно —
    просто первое входящее после нашего сообщения. С разрешённой одновременностью так
    нельзя: два голосовых в работе, и позиционный поиск легко подберёт ЧУЖОЙ ответ,
    то есть пришлёт человеку расшифровку не его записи. Поэтому ищем ответ по связке
    reply_to, а позиционную догадку допускаем только когда наша отправка в работе одна
    и подменить её нечем.
    """
    reply_id = None
    deadline = asyncio.get_event_loop().time() + RELAY_STT_TIMEOUT
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.3)
        if reply_id is None:
            alone = _inflight <= 1
            for m in reversed(await client.get_messages(RELAY_STT_BOT,
                                                        min_id=last_id, limit=20)):
                text = (m.message or "").strip()
                if m.out or not text:
                    continue
                if not (_answers_me(m, my_id) or alone):
                    continue                 # чужой ответ — не трогаем
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
