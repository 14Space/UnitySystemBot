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

import requests

from bot.config import (
    GROQ_API_KEY, GROQ_STT_MODEL, GROQ_STT_TIMEOUT, WHISPER_LANGUAGES,
    WHISPER_MIN_SPEECH_PROB,
)

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


def transcribe(file_path: str) -> str:
    """Расшифровывает файл. Возвращает текст, пустую строку (речи нет) или бросает
    исключение — тогда вызывающий каскад пойдёт к следующему способу."""
    with open(file_path, "rb") as f:
        resp = requests.post(
            _API,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": (os.path.basename(file_path), f, "audio/ogg")},
            # Подробный ответ нужен ради no_speech_prob: без него тишину не отличить.
            data={"model": GROQ_STT_MODEL, "response_format": "verbose_json"},
            timeout=GROQ_STT_TIMEOUT,
        )
    resp.raise_for_status()
    data = resp.json()

    segments = data.get("segments") or []
    if segments:
        # Берём максимум по сегментам: достаточно одного «пустого», чтобы насторожиться,
        # но если речь есть хоть где-то — максимум останется низким только при реальной речи.
        quiet = min(float(s.get("no_speech_prob") or 0.0) for s in segments)
        if quiet > (1.0 - WHISPER_MIN_SPEECH_PROB):
            logger.info("Groq: речи не распознано (no_speech=%.2f) — молчу", quiet)
            return ""

    lang = _LANG_CODE.get(str(data.get("language") or "").lower(), "")
    if lang and _ALLOWED and lang not in _ALLOWED and lang != "en":
        # Язык явно не наш и не английский — почти всегда признак выдумки на шуме.
        logger.info("Groq: язык '%s' не из ожидаемых — молчу", lang)
        return ""

    return (data.get("text") or "").strip()
