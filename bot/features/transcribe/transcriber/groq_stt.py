"""
Расшифровка через Groq — та же модель whisper-large-v3, но на их железе.

Почему основной путь именно облако: на одном и том же голосовом Groq укладывается в
0.45с против 1.12с на нашей видеокарте, текст слово в слово тот же. Заодно видеокарта
остаётся свободной, а её пропажа из WSL (что уже случалось) перестаёт быть аварией.

Защиту от фантомов приходится повторять здесь: на тишине Whisper выдумывает фразы
(проверено — на тишине и на чистом тоне Groq вернул «you»). Спасает поле no_speech_prob,
которое приходит в подробном ответе: у настоящей речи оно около 0.01, у тишины 0.7+.
"""
import logging
import os


from bot.config import (
    GROQ_API_KEY, GROQ_STT_MODEL, GROQ_STT_TIMEOUT, WHISPER_LANGUAGES,
    WHISPER_MIN_SPEECH_PROB, GROQ_STT_PROMPT, GROQ_STT_TEMPERATURE, GROQ_STT_LANGUAGE,
    GROQ_STT_RETRY_BELOW,
)
from bot.utils import net

logger = logging.getLogger(__name__)

_API = "https://api.groq.com/openai/v1/audio/transcriptions"
_ALLOWED = [x.strip() for x in WHISPER_LANGUAGES.split(",") if x.strip()]
# Groq называет язык словом («Russian»), а у нас в настройках коды («ru»).
_LANG_CODE = {
    "russian": "ru", "ukrainian": "uk", "belarusian": "be", "bulgarian": "bg",
    "english": "en", "kazakh": "kk",
}


def available() -> bool:
    return bool(GROQ_API_KEY)


def _ask(file_path: str, language: str = "") -> dict:
    """Один запрос к Groq. language пустой — пусть определяет сам."""
    # Подробный ответ нужен ради no_speech_prob и avg_logprob: по первому отличаем
    # тишину, по второму понимаем, что разбор не удался.
    payload = {
        "model": GROQ_STT_MODEL,
        "response_format": "verbose_json",
        # Ноль догадок: берём самый вероятный вариант, а не «красивый».
        "temperature": GROQ_STT_TEMPERATURE,
    }
    # Подсказка о стиле речи. Whisper выбирает между похоже звучащими словами по тому,
    # что уместнее в тексте такого рода, и без подсказки уходит в книжный язык: в
    # живом голосовом «кента» стало «китом», «ссылки» — «руками», а «тепличные
    # условия» — «типичными». Подсказка возвращает его в разговорный регистр.
    if GROQ_STT_PROMPT:
        payload["prompt"] = GROQ_STT_PROMPT
    if language:
        payload["language"] = language

    with open(file_path, "rb") as f:
        resp = net.session().post(
            _API,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": (os.path.basename(file_path), f, "audio/ogg")},
            data=payload,
            timeout=GROQ_STT_TIMEOUT,
        )
    resp.raise_for_status()
    return resp.json()


def _confidence(data: dict) -> float:
    """Средняя уверенность разбора (avg_logprob по кускам). 0 — нет данных."""
    values = [float(s.get("avg_logprob") or 0.0) for s in (data.get("segments") or [])]
    return sum(values) / len(values) if values else 0.0


def transcribe(file_path: str) -> str:
    """Расшифровка файла: только текст (пустая строка = речи нет)."""
    return transcribe_detailed(file_path)[0]


def transcribe_detailed(file_path: str) -> tuple[str, float | None]:
    """То же, но вместе с УВЕРЕННОСТЬЮ разбора.

    Уверенность нужна каскаду: разобрали мутно — есть смысл спросить следующий способ,
    даже если этот формально не упал. Пустая строка (речи нет) уверенности не имеет —
    там нечего улучшать, и переспрашивать бессмысленно.
    """
    data = _ask(file_path, GROQ_STT_LANGUAGE)

    # Язык МЫ НЕ НАВЯЗЫВАЕМ: бот многоязычный, и «всегда русский» превратил бы
    # украинскую и английскую речь в кашу. Но если облако определило язык, которого мы
    # не ждём, или разобрало неуверенно — перечитываем на основном языке. Ровно так
    # поступает наша локальная модель, и именно этого шага облаку не хватало.
    raw_lang = str(data.get("language") or "").lower()
    lang_code = _LANG_CODE.get(raw_lang, "")
    primary = _ALLOWED[0] if _ALLOWED else ""
    if primary and not GROQ_STT_LANGUAGE:
        # Чужим считаем и язык, которого нет в нашем словаре: если облако решило, что
        # это хорватский или польский, речь почти наверняка русская, просто разобранная
        # не теми правилами. Раньше такие случаи проходили мимо — словарь знает всего
        # шесть языков, и всё остальное молча признавалось «своим».
        foreign = bool(raw_lang) and (
            not lang_code or (lang_code not in _ALLOWED and lang_code != "en"))
        unsure = _confidence(data) < GROQ_STT_RETRY_BELOW
        if foreign or unsure:
            logger.info("Groq: перечитываю как «%s» (язык '%s', уверенность %.2f)",
                        primary, raw_lang or "?", _confidence(data))
            retry = _ask(file_path, primary)
            # Что считать «лучше», зависит от причины повтора:
            #   • язык был чужой — верим повтору, даже если уверенность та же: разбор
            #     не теми правилами бывает уверенным и при этом бессмысленным;
            #   • разбор был просто мутный — берём повтор, только если он честно
            #     увереннее, иначе первый вариант и был лучшим из возможного.
            better = (_confidence(retry) >= _confidence(data) if foreign
                      else _confidence(retry) > _confidence(data))
            if better:
                data = retry

    segments = data.get("segments") or []
    if segments:
        # Берём максимум по сегментам: достаточно одного «пустого», чтобы насторожиться,
        # но если речь есть хоть где-то — максимум останется низким только при реальной речи.
        quiet = min(float(s.get("no_speech_prob") or 0.0) for s in segments)
        if quiet > (1.0 - WHISPER_MIN_SPEECH_PROB):
            logger.info("Groq: речи не распознано (no_speech=%.2f) — молчу", quiet)
            return "", None

    lang = _LANG_CODE.get(str(data.get("language") or "").lower(), "")
    if lang and _ALLOWED and lang not in _ALLOWED and lang != "en":
        # Язык явно не наш и не английский — почти всегда признак выдумки на шуме.
        logger.info("Groq: язык '%s' не из ожидаемых — молчу", lang)
        return "", None

    return (data.get("text") or "").strip(), _confidence(data)
