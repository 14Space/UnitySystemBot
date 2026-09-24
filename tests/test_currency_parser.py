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


def test_long_post_up_to_telegram_limit_is_parsed_and_fast():
    """Сумму ищем и в длинном посте – до предела сообщения Telegram (4096). Разбор при
    этом обязан оставаться быстрым и на подобранной строке: одной регулярке по всему
    тексту на «111.111.111…» нужны были секунды, и бот стоял для всех."""
    import time
    from bot.features.currency import parser

    post = "Итоги месяца. " * 250 + "Продал за 1 200$ в итоге."
    assert len(post) <= parser.MAX_TEXT
    assert parser.parse(post)[:2] == (1200.0, "USD")

    worst = 0.0
    for evil in ("111." * 1024, "1 " * 2048, "$1" * 2048, "$ " * 1000 + "1" * 2000):
        parser._parse.cache_clear()
        t0 = time.perf_counter()
        parser.parse(evil[:parser.MAX_TEXT])
        worst = max(worst, time.perf_counter() - t0)
    assert worst < 0.2, f"разбор снова медленный: {worst:.2f}с"
