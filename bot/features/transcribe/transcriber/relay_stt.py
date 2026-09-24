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
import subprocess
import uuid

from bot.utils import ffmpeg, files
from bot.config import (
    DOWNLOADS_DIR,
    RELAY_STT_BOT, RELAY_STT_SESSION, RELAY_STT_TIMEOUT,
    RELAY_STT_CONCURRENCY, RELAY_STT_MIN_GAP,
    TELEGRAM_API_ID, TELEGRAM_API_HASH,
)

logger = logging.getLogger(__name__)

# Подтверждение приёма, а не результат: увидев такое, продолжаем ждать правку.
_ACK = re.compile(r"принято|обрабат|получен|ожидай|подожд|секунд", re.I)

# Отказ чужого бота приходит обычным сообщением, а не ошибкой, и внешне ничем не
# отличается от расшифровки. Раньше мы такой ответ показывали человеку — 20.09.2026
# на видео-кружок бот процитировал «Не удалось ничего распознать. Вы прислали аудио
# без речи.», будто это и есть сказанные слова.
#
# Узнаём отказ ТОЛЬКО в начале короткого сообщения. Те же слова звучат и в живой
# речи: «Слушай, у меня ошибка в отчёте» — это расшифровка, а не отказ, и потерять
# её было бы хуже исходной беды. Заготовка бота с них начинается, человек — почти
# никогда. Впереди допускаем значки: бот любит начать с «❌».
_NO_SPEECH = re.compile(
    r"\W*(не удалось (ничего )?(распознать|расслышать)"
    r"|не (смог|удаётся|удается|могу) (ничего )?(распознать|расслышать)"
    r"|(в )?(аудио|записи|файле|сообщении) (нет|без) речи"
    r"|аудио без речи|речь не (найден|обнаруж)"
    r"|тишина\W*$|no speech)", re.I)
# Отказ по другой причине (формат, размер, лимит) — это сбой: пусть каскад так его и
# запишет, а человек увидит честное «расшифровка не работает», а не пустоту.
_REFUSAL = re.compile(
    r"\W*((произошла )?ошибк|сбой|файл слишком|слишком (длин|больш|велик)"
    r"|превышен|лимит|попробуй(те)? (позже|ещ)"
    r"|(формат )?не поддерживается|не удалось (обработать|скачать)"
    r"|сервис недоступен|недоступен)", re.I)
# Длиннее — это уже расшифровка, в которой такие слова просто прозвучали. Отказы
# чужого бота — короткие заготовки в одну-две фразы.
_VERDICT_MAX = 160

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
# Сколько ждём ffmpeg на сборку голосового. Кодирование идёт в десятки раз быстрее
# реального времени, так что минуты хватает и на часовую запись.
_CONVERT_TIMEOUT = 60


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


def _as_voice(path: str) -> tuple[str, bool]:
    """Перегоняет запись в ogg/opus — формат настоящего голосового.

    Чужой бот разбирает голосовые, а к нам он попадает уже ПОСЛЕ подготовки звука,
    которая отдаёт wav (см. audio_prep). Голосовым Telegram такой файл не считает —
    он уходит вложением, и бот отвечает «Вы прислали аудио без речи». Когда каскад
    писали, подготовки ещё не было, и голосовое доезжало до него как есть.

    Возвращает (путь, наш_ли_это_файл) — как audio_prep.prepare. Конвертация не
    обязана удаться: не вышла — шлём что есть, хуже прежнего не будет.
    """
    if path.lower().endswith((".ogg", ".oga", ".opus")):
        return path, False
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)    # на свежей установке папки ещё нет
    out = os.path.join(DOWNLOADS_DIR, f"relay_{uuid.uuid4().hex[:8]}.ogg")
    cmd = ["ffmpeg", "-y", "-i", path, "-ac", "1", "-ar", "48000",
           "-c:a", "libopus", "-b:a", "32k", out]
    try:
        res = ffmpeg.run(cmd, timeout=_CONVERT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        logger.info("Не собрал голосовое для бота-расшифровщика (%s) — шлю как есть",
                    type(e).__name__)
        return path, False
    if res.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        logger.info("Не собрал голосовое для бота-расшифровщика (ffmpeg %s) — шлю как есть",
                    res.returncode)
        _drop(out)
        return path, False
    return out, True


def _drop(path: str) -> None:
    files.remove(path)


def _as_result(text: str) -> str:
    """Отделяет расшифровку от отказа. Пустая строка = речи нет, исключение = сбой."""
    text = (text or "").strip()
    if len(text) <= _VERDICT_MAX:
        if _NO_SPEECH.match(text):
            logger.info("Бот-расшифровщик: речи нет (%r)", text[:80])
            return ""
        if _REFUSAL.match(text):
            raise RuntimeError(f"бот-расшифровщик отказал: {text[:80]}")
    return text


async def transcribe(file_path: str) -> str:
    """Шлёт файл чужому боту и ждёт расшифровку. Бросает исключение, если не вышло."""
    global _inflight
    client = await _get_client()
    voice, ours = await asyncio.to_thread(_as_voice, file_path)
    try:
        async with (_gate or _NoGate()):
            await _space_out()
            last = await client.get_messages(RELAY_STT_BOT, limit=1)
            last_id = last[0].id if last else 0
            sent = await client.send_file(RELAY_STT_BOT, voice, voice_note=True)
            my_id = getattr(sent, "id", 0)
            _inflight += 1
            try:
                return _as_result(await _await_answer(client, last_id, my_id))
            finally:
                _inflight -= 1
    finally:
        if ours:
            _drop(voice)


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
