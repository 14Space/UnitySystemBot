"""Что бот показывает после расшифровки, а на что молчит.

Правило простое: если речи не было, бот НЕ отвечает ничего. Любой ответ движка,
в котором нет ни одной буквы («...», «-», «♪»), — это признак, что речи не было,
а не расшифровка. Именно так 18.09.2026 бот ответил «...» на запись, где мимо
проходили люди с инструментами.
"""
import pytest

from bot.features.transcribe.transcriber.whisper_transcriber import _clean


@pytest.mark.parametrize("text", [
    "...",
    "…",
    " . . . ",
    "-",
    "—",
    "♪♪♪",
    "!?",
    "",
    "   ",
])
def test_no_letters_means_silence(text):
    assert _clean(text) == ""


@pytest.mark.parametrize("text", [
    "you",
    "You.",
    "Thank you",
    "Продолжение следует",
    "спасибо за просмотр",
])
def test_typical_phantoms_are_dropped(text):
    assert _clean(text) == ""


@pytest.mark.parametrize("text, expected", [
    ("Привет, как дела?", "Привет, как дела?"),
    ("Да", "Да"),                                  # короткий, но настоящий ответ
    ("Ок!", "Ок!"),
    ("Позвони мне в 5", "Позвони мне в 5"),
    ("  двойные   пробелы  ", "двойные пробелы"),
])
def test_real_speech_passes_through(text, expected):
    assert _clean(text) == expected


def test_subtitle_credits_are_cut_but_speech_stays():
    assert _clean("Привет. Субтитры сделал DimaTorzok") == "Привет."


def test_credits_only_becomes_silence():
    assert _clean("Субтитры сделал DimaTorzok") == ""


def test_every_method_goes_through_the_same_filter():
    """Ответ ЛЮБОГО способа должен проходить очистку.

    Раньше её проходили только своя модель и чужой бот, а Groq — основной способ —
    отдавал текст напрямую. Тест держит это свойство: очистка стоит в одной точке
    каскада, через которую возвращаются все способы.
    """
    import asyncio
    from bot.features.transcribe.transcriber import cascade

    async def scenario(answer):
        async def fake(path):
            # Способ возвращает (текст, уверенность); None = «уверенность неизвестна».
            return answer, None
        cascade._METHODS["fake"] = fake
        try:
            return await cascade.transcribe_audio("не важно")
        finally:
            cascade._METHODS.pop("fake", None)

    original = cascade.STT_ORDER
    cascade.STT_ORDER = ["fake"]
    try:
        assert asyncio.run(scenario("...")) == ""
        assert asyncio.run(scenario("Привет")) == "Привет"
    finally:
        cascade.STT_ORDER = original
