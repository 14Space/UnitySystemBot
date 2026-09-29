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


def _send_video_with_file(tmp_path):
    """Настоящий запрос отправки видео с ЗАЛИВКОЙ файла."""
    from aiogram.methods import SendVideo
    from aiogram.types import FSInputFile

    probe = tmp_path / "v.mp4"
    probe.write_bytes(b"0" * 10)
    return SendVideo(chat_id=1, video=FSInputFile(str(probe)))


def _send_video_by_file_id():
    """Тот же метод, но файл уже у Telegram — заливки нет."""
    from aiogram.methods import SendVideo
    return SendVideo(chat_id=1, video="BAADBAADfile_id")


def test_upload_is_recognized(tmp_path):
    assert retry._is_upload(_send_video_with_file(tmp_path))
    assert not retry._is_upload(_send_video_by_file_id())
    assert not retry._is_upload(_FakeMethod())


def test_network_error_during_upload_is_not_retried(tmp_path, _no_real_sleep):
    """Обрыв посреди заливки — повтор означал бы вторую полную заливку того же файла."""
    from aiogram.exceptions import TelegramNetworkError

    calls = []

    async def make_request(bot, method):
        calls.append(1)
        raise TelegramNetworkError(method=_FakeMethod(), message="обрыв")

    with pytest.raises(TelegramNetworkError):
        asyncio.run(RetryAfterMiddleware()(make_request, None, _send_video_with_file(tmp_path)))
    assert len(calls) == 1


def test_flood_limit_during_upload_is_retried_once(tmp_path, _no_real_sleep):
    calls = []

    async def make_request(bot, method):
        calls.append(1)
        if len(calls) == 1:
            raise TelegramRetryAfter(method=_FakeMethod(), message="flood", retry_after=3)
        return "ушло"

    result = asyncio.run(
        RetryAfterMiddleware()(make_request, None, _send_video_with_file(tmp_path)))
    assert result == "ушло" and len(calls) == 2


def test_file_url_send_is_not_retried_on_network_error(_no_real_sleep):
    """«file://» – сервер Bot API сам заливает фильм; после обрыва первая отправка
    могла дойти, и повтор прислал бы фильм дважды."""
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendVideo

    method = SendVideo(chat_id=1, video="file:///data/film.mp4")
    assert retry.sends_file(method) and not retry.sends_file(_send_video_by_file_id())
    calls = []

    async def make_request(bot, method):
        calls.append(1)
        raise TelegramNetworkError(method=_FakeMethod(), message="Request timeout error")

    with pytest.raises(TelegramNetworkError):
        asyncio.run(RetryAfterMiddleware()(make_request, None, method))
    assert len(calls) == 1


def test_file_send_waits_longer():
    """Отправке файла – долгий срок ожидания, остальным запросам – обычный."""
    from aiogram.methods import SendVideo, SendMessage
    from bot import main

    seen = []

    async def fake_make_request(self, bot, method, timeout=None):
        seen.append(timeout)

    session = main._Session()
    orig = main.AiohttpSession.make_request
    main.AiohttpSession.make_request = fake_make_request
    try:
        asyncio.run(session.make_request(None, SendVideo(chat_id=1, video="file:///f.mp4")))
        asyncio.run(session.make_request(None, SendMessage(chat_id=1, text="привет")))
    finally:
        main.AiohttpSession.make_request = orig
    assert seen == [main._FILE_SEND_TIMEOUT, None]
