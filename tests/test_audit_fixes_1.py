"""Первая группа правок по аудиту: то, что роняло бота или стоило денег.

Каждый тест назван бедой, а не функцией: через полгода важно понимать, ЗАЧЕМ он есть.
"""
import ast
import asyncio
import importlib
import pathlib
import time

import pytest


def test_one_message_can_no_longer_freeze_the_bot():
    """S1. Строка «1 1 1 1…» разбиралась 3.25с, а разбор зовут на каждое сообщение —
    любой участник группы мог одним сообщением остановить бота секунд на десять."""
    from bot.features.currency import parser

    text = "1 " * 2000
    start = time.monotonic()
    assert parser.parse(text) is None
    assert time.monotonic() - start < 0.2          # было 3.25с


@pytest.mark.parametrize("text, expected", [
    ("1 234 567,89 грн", (1234567.89, "UAH", None)),
    ("10 000$", (10000.0, "USD", None)),
    ("12,5 евро", (12.5, "EUR", None)),
    ("5к грн", (5000.0, "UAH", None)),
])
def test_real_amounts_still_parse(text, expected):
    """Ускорение не должно стоить нам живых форматов записи суммы."""
    from bot.features.currency import parser
    assert parser.parse(text) == expected


def test_long_text_is_not_parsed_at_all():
    from bot.features.currency import parser
    assert parser.parse("а" * (parser.MAX_TEXT + 1) + " 100$") is None


def test_feature_is_computed_once_per_update():
    """S1. Разбор зовут три посредника подряд; считать его надо один раз."""
    from bot.middlewares import routing

    calls = []
    original = routing._msg_feature
    routing._msg_feature = lambda m: calls.append(1) or "currency"
    try:
        data = {}
        message = object()
        routing.feature_of(message, data)
        routing.is_request_called = routing.feature_of(message, data)
        routing.feature_of(message, data)
    finally:
        routing._msg_feature = original
    assert len(calls) == 1


def test_premium_survives_a_first_time_buyer(tmp_path, monkeypatch):
    """B1. Оплатил и не получил Premium: записи о человеке в базе ещё не было, а
    set_premium в этом случае молча ничего не делал. Нажатие 🔒 — это callback,
    записи он не создаёт, так что сценарий был совершенно обычным."""
    import bot.config as config
    import bot.database as db

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
    db = importlib.reload(db)

    async def scenario():
        await db.init_db()
        import bot.database.repository as repo
        importlib.reload(repo)
        async with db.SessionLocal() as s:
            await repo.set_premium(s, 4242, True)     # человека в базе НЕТ
            got = await repo.is_premium(s, 4242)
        await db.engine.dispose()
        return got

    assert asyncio.run(scenario()) is True


def test_audio_prep_does_not_block_the_loop():
    """P1. Подготовка звука — это ffmpeg до минуты работы; в цикле событий она
    останавливала бота целиком на каждое голосовое."""
    source = (pathlib.Path(__file__).resolve().parent.parent
              / "bot/features/transcribe/transcriber/cascade.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    prepares = [n for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and n.attr == "prepare"]
    assert prepares, "вызов подготовки исчез — тест устарел"
    # Каждый вызов обёрнут в to_thread: ищем его в том же выражении.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and any(
                isinstance(a, ast.Attribute) and a.attr in ("prepare", "cleanup")
                for a in ast.walk(node)):
            names = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            assert "to_thread" in names, "подготовка звука снова идёт в цикле событий"


def test_watchdog_release_does_not_double_count(monkeypatch):
    """B4. Сторож отпускал зависший слот, а потом владелец отпускал его ещё раз —
    счётчик уходил ниже правды, и потолки перестали держать вовсе."""
    from bot.utils import limits

    fresh = limits._SmartLimiter()
    monkeypatch.setattr(limits, "_limiter", fresh)
    monkeypatch.setattr(limits, "MAX_HOLD_SECONDS", 0)

    async def scenario():
        token = await limits.acquire(limits.HEAVY)
        assert limits.state()[limits.HEAVY]["active"] == 1
        # Сторож забирает слот (проверка условия — то место, где это происходит).
        assert fresh._forget_lost(limits.HEAVY) == 1
        # Владелец возвращается и отпускает СВОЙ слот — счётчик не должен уйти в минус.
        await limits.release(limits.HEAVY, token)
        assert limits.state()[limits.HEAVY]["active"] == 0
        # И потолок по-прежнему на месте.
        tokens = [await limits.acquire(limits.HEAVY) for _ in range(limits.HEAVY_MAX)]
        assert limits.queue_is_full(limits.HEAVY)
        return tokens

    asyncio.run(scenario())


def test_error_text_cannot_break_the_daily_report():
    """S8. Ошибка вида «<Response [403]>» в тексте пункта ломала разбор HTML, и отчёт
    не приходил ЦЕЛИКОМ — вместо одной красной строки."""
    from bot.features.common.healthcheck import format_health

    results = [{"name": "TikTok видео", "platform": "TikTok", "idx": 0,
                "state": "fail", "detail": "HTTPError: <Response [403]>", "sec": 1.0}]
    text = format_health(results, ["TikTok"], lang="ru")
    assert "<Response" not in text
    assert "&lt;Response [403]&gt;" in text


def test_bot_api_port_is_not_public():
    """S4. Порт локального Bot API был открыт на 0.0.0.0 — сервер отвечал из
    интернета (проверено запросом с чужой машины), а публикация порта в docker
    обходит ufw, так что firewall от этого не спасал."""
    root = pathlib.Path(__file__).resolve().parent.parent
    for name in ("docker-compose.yml",):
        text = (root / name).read_text(encoding="utf-8")
        assert '"8081:8081"' not in text, f"{name}: порт снова открыт наружу"
        assert '"127.0.0.1:8081:8081"' in text, f"{name}: порт не привязан к localhost"
