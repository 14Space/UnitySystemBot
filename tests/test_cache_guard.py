"""Самолечение кэша: протухший file_id не должен оставлять человека без файла."""
import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest

from bot.utils.cache_guard import is_dead_file_id, send_cached_or_drop


class _FakeMethod:
    """Заглушка вместо запроса к Telegram — TelegramBadRequest требует метод."""


def _bad_request(text: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=_FakeMethod(), message=text)


@pytest.mark.parametrize("text", [
    "Bad Request: wrong file identifier/HTTP URL specified",
    "Bad Request: wrong remote file identifier specified",
    "Bad Request: file reference expired",
])
def test_dead_file_id_is_recognized(text):
    assert is_dead_file_id(_bad_request(text))


@pytest.mark.parametrize("error", [
    _bad_request("Bad Request: chat not found"),
    _bad_request("Bad Request: message caption is too long"),
    RuntimeError("wrong file identifier"),        # не от Telegram — не наш случай
])
def test_other_errors_are_not_treated_as_dead_cache(error):
    assert not is_dead_file_id(error)


def test_successful_send_reports_true(monkeypatch):
    async def send():
        return None

    assert asyncio.run(send_cached_or_drop(send, "http://x", "v")) is True


def test_dead_cache_is_cleared_and_reports_false(monkeypatch):
    cleared = []

    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    async def fake_clear(session, url, quality):
        cleared.append((url, quality))

    monkeypatch.setattr("bot.utils.cache_guard.SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr("bot.utils.cache_guard.clear_cache_entry", fake_clear)

    async def send():
        raise _bad_request("Bad Request: wrong file identifier/HTTP URL specified")

    assert asyncio.run(send_cached_or_drop(send, "http://x", "v")) is False
    assert cleared == [("http://x", "v")]


def test_foreign_errors_are_reraised(monkeypatch):
    async def send():
        raise _bad_request("Bad Request: chat not found")

    with pytest.raises(TelegramBadRequest):
        asyncio.run(send_cached_or_drop(send, "http://x", "v"))
