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
    а пользователь будет ждать втрое дольше. Поэтому на пустом ответе останавливаемся —
    но не стираем им уже разобранный текст, если за вторым мнением мы пришли сами.
"""
import asyncio
import logging

from bot.config import (
    STT_ORDER, STT_ESCALATE_BELOW, RELAY_IN_GROUPS, STT_VAD, STT_MIN_SPEECH_SECONDS,
)
from bot.features.transcribe import audio_prep, vad

logger = logging.getLogger(__name__)


def _local_sync(path: str) -> str:
    from bot.features.transcribe.transcriber import whisper_transcriber as w
    return w.transcribe(path)


async def _run_local(path: str) -> tuple[str, float | None]:
    # У локальной модели есть и перебор вариантов, и фильтр тишины — её разбор мы
    # считаем достаточным и не переспрашиваем (уверенность = None).
    return await asyncio.to_thread(_local_sync, path), None


async def _run_groq(path: str) -> tuple[str, float | None]:
    from bot.features.transcribe.transcriber import groq_stt
    if not groq_stt.available():
        raise RuntimeError("нет ключа Groq")
    return await asyncio.to_thread(groq_stt.transcribe_detailed, path)


def _clean(text: str) -> str:
    """Общая очистка результата — одна на все способы (см. whisper_transcriber).

    Отдельной функцией, потому что раньше её проходили только своя модель и чужой
    бот, а ответ Groq — основного способа! — уходил человеку как есть. Из-за этого
    на записи без речи (мимо проходили музыканты) бот отвечал «...» вместо молчания.
    """
    from bot.features.transcribe.transcriber.whisper_transcriber import _clean as clean
    return clean(text or "")


async def _run_relay(path: str) -> tuple[str, float | None]:
    from bot.features.transcribe.transcriber import relay_stt
    if not relay_stt.available():
        raise RuntimeError("нет сессии-посредника")
    # Чужой бот вдобавок отвечает «...», когда сам ничего не разобрал, — очистка
    # считает такой ответ молчанием. Уверенности он не сообщает: берём как есть.
    return await relay_stt.transcribe(path), None


_METHODS = {"groq": _run_groq, "local": _run_local, "relay": _run_relay}


async def transcribe_audio(file_path: str, *, private: bool = True) -> str:
    """Расшифровка каскадом. Пустая строка = речи нет.

    private=False – запись из группы: посредника (чужой бот через личный аккаунт)
    зовём, только если RELAY_IN_GROUPS включён (по умолчанию включён).

    К следующему способу переходим по ДВУМ причинам:
      • сбой (сеть, лимит, мёртвая сессия) — как было всегда;
      • мутный разбор. Способ отработал, но сам сообщает, что не уверен. Раньше такой
        ответ уходил человеку как есть, хотя рядом стоял движок, который разобрал бы
        лучше. Теперь простые записи остаются быстрыми, а сложные доезжают до того,
        кто с ними справится.

    Пустой ответ («речи нет») причиной для перехода не считается: это законный
    результат, и остальные способы честно вернут ту же пустоту.
    """
    # Сначала – есть ли речь вообще. Тишину и шум распознаванию не отдаём: на них оно
    # выдумывает подписи к видео, а собственному сигналу облака о тишине верить нельзя
    # (см. vad.py). Заодно не тратим лимит облака и не шлём пустое посреднику.
    if STT_VAD:
        speech = await asyncio.to_thread(vad.speech_seconds, file_path)
        if speech is not None and speech < STT_MIN_SPEECH_SECONDS:
            logger.info("Речи в записи нет (%.2fс по детектору) — молчу", speech)
            return ""
    # Подготовка — это запуск ffmpeg, до минуты работы. В цикле событий ей делать
    # нечего: пока она считала, бот не отвечал НИКОМУ — ни на ссылки, ни на кнопки,
    # и так на каждое голосовое. Уносим в поток, как и сами способы распознавания.
    prepared, ours = await asyncio.to_thread(audio_prep.prepare, file_path)
    try:
        skip = () if private or RELAY_IN_GROUPS else ("relay",)
        return await _cascade(prepared, skip)
    finally:
        await asyncio.to_thread(audio_prep.cleanup, prepared, ours)


async def _cascade(file_path: str, skip: tuple[str, ...] = ()) -> str:
    errors = []
    fallback = None          # лучший из «мутных» ответов, на случай если лучше не будет
    for name in STT_ORDER:
        if name in skip:
            continue
        run = _METHODS.get(name)
        if run is None:
            logger.warning("Неизвестный способ расшифровки в STT_ORDER: %s", name)
            continue
        try:
            text, confidence = await run(file_path)
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            logger.warning("Расшифровка через «%s» не удалась, пробую следующий способ",
                           name, exc_info=True)
            continue
        if name != STT_ORDER[0]:
            logger.info("Расшифровка выполнена запасным способом «%s»", name)

        # Очистка — здесь, в одной точке на все способы: так новый движок не сможет
        # появиться в обход правил о том, что показывать человеку.
        text = _clean(text)
        if not text:
            # «Речи нет» — законный ответ, переспрашивать бессмысленно. Но если
            # предыдущий способ речь всё-таки разобрал, пусть и неуверенно, пустота
            # следующего её не отменяет: мы пришли сюда как раз за вторым мнением,
            # и промолчать в ответ на явно сказанные слова — худший из исходов.
            return fallback or ""

        if _good_enough(confidence):
            return text
        logger.info("«%s» разобрал неуверенно (%.2f) — пробую следующий способ",
                    name, confidence)
        if fallback is None:
            fallback = text

    if fallback:
        # Никто не разобрал уверенно — отдаём первый вариант: он всё равно лучше, чем
        # «не удалось ничего распознать».
        logger.info("Уверенно не разобрал никто — отдаю первый вариант")
        return fallback
    raise RuntimeError("все способы расшифровки отказали -> " + "; ".join(errors))


def _good_enough(confidence: float | None) -> bool:
    """Достаточно ли уверенно разобрано, чтобы не спрашивать следующий способ."""
    if confidence is None or not STT_ESCALATE_BELOW:
        return True          # способ не сообщает уверенность или порог выключен
    return confidence >= STT_ESCALATE_BELOW
