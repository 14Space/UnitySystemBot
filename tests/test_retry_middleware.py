"""Повтор запросов, когда Telegram просит подождать."""
import asyncio

import pytest
from aiogram.exceptions import TelegramRetryAfter, TelegramServerError

from bot.middlewares import retry
from bot.middlewares.retry import RetryAfterMiddleware


class _FakeMethod:
    pass


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Тест не должен реально ждать — подменяем паузу на мгновенную."""
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(retry.asyncio, "sleep", fake_sleep)
    return slept


def test_retries_after_flood_limit(_no_real_sleep):
    calls = []

    async def make_request(bot, method):
        calls.append(1)
        if len(calls) == 1:
            raise TelegramRetryAfter(method=_FakeMethod(), message="flood", retry_after=7)
        return "ушло"

    result = asyncio.run(RetryAfterMiddleware()(make_request, None, _FakeMethod()))
    assert result == "ушло"
    assert len(calls) == 2
    assert _no_real_sleep == [7.5]        # ждём столько, сколько просили, плюс запас


def test_gives_up_after_too_many_attempts(_no_real_sleep):
    async def make_request(bot, method):
        raise TelegramRetryAfter(method=_FakeMethod(), message="flood", retry_after=1)

    with pytest.raises(TelegramRetryAfter):
        asyncio.run(RetryAfterMiddleware()(make_request, None, _FakeMethod()))


def test_does_not_wait_absurdly_long(_no_real_sleep):
    """Просьба ждать полчаса — не наш случай: лучше честная ошибка сразу."""
    async def make_request(bot, method):
        raise TelegramRetryAfter(method=_FakeMethod(), message="flood", retry_after=999)

    with pytest.raises(TelegramRetryAfter):
        asyncio.run(RetryAfterMiddleware()(make_request, None, _FakeMethod()))
    assert _no_real_sleep == []


def test_retries_telegram_server_error(_no_real_sleep):
    calls = []

    async def make_request(bot, method):
        calls.append(1)
        if len(calls) < 3:
            raise TelegramServerError(method=_FakeMethod(), message="502")
        return "ушло"

    assert asyncio.run(RetryAfterMiddleware()(make_request, None, _FakeMethod())) == "ушло"
    assert len(calls) == 3
