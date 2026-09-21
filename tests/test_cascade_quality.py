"""Каскад переключается не только на сбое, но и на плохом качестве разбора.

Смысл: простое голосовое остаётся быстрым (облако, полсекунды), а сложное — записанное
на ветру, со сленгом — доезжает до движка, который справится лучше. Раньше мутный
ответ уходил человеку как есть, хотя рядом стоял способ получше.
"""
import asyncio

import pytest

from bot.features.transcribe.transcriber import cascade


@pytest.fixture(autouse=True)
def _no_audio_prep(monkeypatch):
    """Подготовку звука здесь не проверяем — она в своём тесте."""
    monkeypatch.setattr(cascade.audio_prep, "prepare", lambda path: (path, False))
    monkeypatch.setattr(cascade.audio_prep, "cleanup", lambda path, ours: None)


def _run(order, methods):
    monkey = dict(cascade._METHODS)
    cascade._METHODS.update(methods)
    original = cascade.STT_ORDER
    cascade.STT_ORDER = order
    try:
        return asyncio.run(cascade.transcribe_audio("x.ogg"))
    finally:
        cascade._METHODS.clear()
        cascade._METHODS.update(monkey)
        cascade.STT_ORDER = original


def test_confident_answer_stops_the_cascade():
    calls = []

    async def first(path):
        calls.append("first")
        return "уверенный текст", -0.2

    async def second(path):
        calls.append("second")
        return "второй", None

    assert _run(["a", "b"], {"a": first, "b": second}) == "уверенный текст"
    assert calls == ["first"]          # второй способ не трогали


def test_unsure_answer_escalates():
    async def first(path):
        return "мутный текст", -0.9

    async def second(path):
        return "текст получше", None

    assert _run(["a", "b"], {"a": first, "b": second}) == "текст получше"


def test_first_answer_is_kept_when_nobody_is_confident():
    """Если увереннее не разобрал никто — отдаём первый вариант, а не ошибку."""
    async def first(path):
        return "хоть что-то", -0.9

    async def second(path):
        raise RuntimeError("и этот отказал")

    assert _run(["a", "b"], {"a": first, "b": second}) == "хоть что-то"


def test_silence_does_not_escalate():
    """Пустой ответ — это «речи нет», а не повод гонять запись по всем движкам."""
    calls = []

    async def first(path):
        calls.append("first")
        return "", None

    async def second(path):
        calls.append("second")
        return "выдумка", None

    assert _run(["a", "b"], {"a": first, "b": second}) == ""
    assert calls == ["first"]


def test_failure_still_escalates():
    async def first(path):
        raise RuntimeError("сеть отвалилась")

    async def second(path):
        return "запасной справился", None

    assert _run(["a", "b"], {"a": first, "b": second}) == "запасной справился"


def test_all_failed_raises():
    async def boom(path):
        raise RuntimeError("отказ")

    with pytest.raises(RuntimeError):
        _run(["a"], {"a": boom})


@pytest.mark.parametrize("confidence, expected", [
    (-0.2, True),      # обычная живая речь
    (-0.54, True),     # на границе — ещё принимаем
    (-0.56, False),    # уже мутно
    (None, True),      # способ не сообщает уверенность
])
def test_threshold(confidence, expected):
    assert cascade._good_enough(confidence) is expected


def test_empty_second_opinion_does_not_erase_the_first():
    """20.09.2026: облако разобрало неуверенно, чужой бот ответил «речи нет» — и
    человек не получил ничего, хотя слова в записи были и облако их разобрало."""
    async def first(path):
        return "слова всё-таки были", -0.9

    async def second(path):
        return "", None

    assert _run(["a", "b"], {"a": first, "b": second}) == "слова всё-таки были"
