"""Разбор «100$» и подобного из обычного текста.

Ловушка этой функции — ложные срабатывания: она смотрит КАЖДОЕ сообщение в чате,
и если начнёт видеть валюту там, где её нет, бот полезет отвечать невпопад.
"""
import pytest

from bot.features.currency.parser import parse, fmt_amount


@pytest.mark.parametrize("text, expected", [
    ("100$", (100.0, "USD", None)),
    ("$100", (100.0, "USD", None)),
    ("100 долларов", (100.0, "USD", None)),
    ("5000 грн", (5000.0, "UAH", None)),
    ("5к грн", (5000.0, "UAH", None)),
    ("5млн руб", (5_000_000.0, "RUB", None)),
    ("500 UAH to AED", (500.0, "UAH", "AED")),
    ("5000 грн в шекели", (5000.0, "UAH", "ILS")),
])
def test_parsed(text, expected):
    assert parse(text) == expected


@pytest.mark.parametrize("text", [
    "привет",
    "доллар",                              # валюта без числа — не запрос
    "https://example.com/100$",            # внутри ссылки не ловим
    "",
])
def test_ignored(text):
    assert parse(text) is None


def test_fmt_amount_trims_zeros():
    assert fmt_amount(100.0) == "100"
