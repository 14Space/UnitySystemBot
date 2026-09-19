"""Длинная расшифровка уходит несколькими сообщениями и ничего не теряет.

Получасовое голосовое даёт куда больше 4096 символов — лимита одного сообщения.
Раньше остаток просто отрезался многоточием, и человек не узнавал, что сказано дальше.
"""
import html

import pytest

from bot.features.transcribe.transcribe import _split_for_telegram, _quoted, _ROOM, MAX_TEXT


# Длинные тексты передаём кусочком и множителем: если положить сюда саму строку,
# pytest подставит её целиком в НАЗВАНИЕ теста — а это десятки тысяч символов.
@pytest.mark.parametrize("piece, times", [
    ("Привет. ", 900),         # обычная речь
    ("слово ", 2000),          # без знаков препинания
    ("a", 9000),               # сплошной поток без пробелов
    ("<>&", 3000),             # спецсимволы: после экранирования текст втрое длиннее
])
def test_every_part_fits_a_message(piece, times):
    text = piece * times
    for part in _split_for_telegram(text):
        assert len(_quoted(part)) <= MAX_TEXT


@pytest.mark.parametrize("piece, times", [
    ("Привет. ", 900),
    ("слово ", 2000),
    ("a", 9000),
])
def test_nothing_is_lost(piece, times):
    text = piece * times
    joined = "".join(_split_for_telegram(text))
    # Пробелы на стыках частей съедаются (мы режем по границе) — сверяем сам текст.
    assert joined.replace(" ", "") == text.replace(" ", "")


def test_short_text_stays_one_message():
    assert _split_for_telegram("Коротко и ясно") == ["Коротко и ясно"]


@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_gives_no_parts(text):
    assert _split_for_telegram(text) == []


def test_parts_break_on_sentence_end_when_possible():
    text = ("Первое предложение. " * 300).strip()
    first = _split_for_telegram(text)[0]
    assert first.endswith(".")


def test_no_part_starts_mid_word_for_normal_speech():
    parts = _split_for_telegram("Привет как дела " * 700)
    assert all(not p.startswith(" ") for p in parts)
    assert all(p == p.strip() for p in parts)


def test_escaping_is_accounted_for():
    """Символ «<» после экранирования занимает четыре места — срез по len() врал бы."""
    part = _split_for_telegram("<" * 5000)[0]
    assert len(html.escape(part)) <= _ROOM
