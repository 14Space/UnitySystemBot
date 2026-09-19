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


def _clean(text: str) -> str:
    """Общая очистка результата — одна на все способы (см. whisper_transcriber).

    Отдельной функцией, потому что раньше её проходили только своя модель и чужой
    бот, а ответ Groq — основного способа! — уходил человеку как есть. Из-за этого
    на записи без речи (мимо проходили музыканты) бот отвечал «...» вместо молчания.
    """
    from bot.features.transcribe.transcriber.whisper_transcriber import _clean as clean
    return clean(text or "")


async def _run_relay(path: str) -> str:
    from bot.features.transcribe.transcriber import relay_stt
    if not relay_stt.available():
        raise RuntimeError("нет сессии-посредника")
    # Чужой бот вдобавок отвечает «...», когда сам ничего не разобрал, — очистка
    # считает такой ответ молчанием.
    return await relay_stt.transcribe(path)


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
        # Очистка — здесь, в одной точке на все способы: так новый движок не сможет
        # появиться в обход правил о том, что показывать человеку.
        return _clean(text)
    raise RuntimeError("все способы расшифровки отказали -> " + "; ".join(errors))
