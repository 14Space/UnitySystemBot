"""
Каскад расшифровки: пробуем способы по очереди, пока один не даст результат.

Порядок задаётся STT_ORDER, по умолчанию groq → local → relay:
  • groq  — облако, та же модель whisper-large-v3, самый быстрый (замер 0.45с);
  • local — наша видеокарта, запасной (1.12с). Работает без сети и ничего не отдаёт
            наружу, поэтому именно он страхует облако;
  • relay — чужой бот в Telegram через аккаунт-посредник (2.2с). Последний рубеж:
            медленный, требует живой пользовательской сессии и шлёт голосовое
            постороннему сервису.

Важное различие, из-за которого каскад не так прост, как кажется:
  • ИСКЛЮЧЕНИЕ (сеть отвалилась, кончился лимит, сдохла видеокарта) — переходим
    к следующему способу;
  • ПУСТАЯ строка — это НЕ сбой, а законный ответ «речи нет» (тишина, музыка, шум).
    Гонять такое по всем трём способам бессмысленно: каждый честно вернёт пустоту,
    а пользователь будет ждать втрое дольше. Поэтому на пустом ответе останавливаемся.
"""
import asyncio
import logging

from bot.config import STT_ORDER

logger = logging.getLogger(__name__)


def _local_sync(path: str) -> str:
    from bot.features.transcribe.transcriber import whisper_transcriber as w
    return w.transcribe(path)


async def _run_local(path: str) -> str:
    return await asyncio.to_thread(_local_sync, path)


async def _run_groq(path: str) -> str:
    from bot.features.transcribe.transcriber import groq_stt
    if not groq_stt.available():
        raise RuntimeError("нет ключа Groq")
    return await asyncio.to_thread(groq_stt.transcribe, path)


async def _run_relay(path: str) -> str:
    from bot.features.transcribe.transcriber import relay_stt
    if not relay_stt.available():
        raise RuntimeError("нет сессии-посредника")
    from bot.features.transcribe.transcriber.whisper_transcriber import _clean
    # Текст чужого бота проходит нашу очистку: фразы-титры и мусор убираем так же,
    # как у своих способов — качество вывода остаётся нашим, чей бы ни был движок.
    return _clean(await relay_stt.transcribe(path))


_METHODS = {"groq": _run_groq, "local": _run_local, "relay": _run_relay}


async def transcribe_audio(file_path: str) -> str:
    """Расшифровка с переходом к следующему способу при сбое. Пустая строка = речи нет."""
    errors = []
    for name in STT_ORDER:
        run = _METHODS.get(name)
        if run is None:
            logger.warning("Неизвестный способ расшифровки в STT_ORDER: %s", name)
            continue
        try:
            text = await run(file_path)
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            logger.warning("Расшифровка через «%s» не удалась, пробую следующий способ",
                           name, exc_info=True)
            continue
        if name != STT_ORDER[0]:
            logger.info("Расшифровка выполнена запасным способом «%s»", name)
        return text
    raise RuntimeError("все способы расшифровки отказали -> " + "; ".join(errors))
