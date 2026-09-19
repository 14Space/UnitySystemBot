"""Понятные сообщения об ошибках и очередь задач.

friendly_error переводит техническую ошибку площадки в человеческую фразу. Ошибётся
она — человек получит «не удалось скачать» вместо «нужны свежие куки», и причина
поломки потеряется.
"""
import asyncio

import pytest

from bot.utils import limits
from bot.utils.i18n import t


@pytest.mark.parametrize("message, expected_key", [
    ("Requested format is not available: file is too big", "err_too_large"),
    ("This video is DRM protected", "err_drm"),
    ("Sign in to confirm your age", "err_age"),
    ("Video unavailable", "err_not_found"),
    ("HTTP Error 404: Not Found", "err_not_found"),
    ("Read timed out", "err_network"),
    ("This post is only available to certain audiences", "err_login_required"),
    ("Video is private", "err_private"),
    ("The uploader has not made this video available in your country", "err_geo"),
])
def test_known_errors_get_human_text(message, expected_key):
    assert limits.friendly_error(Exception(message), "ru") == t(expected_key, "ru")


def test_unknown_error_falls_back_to_generic():
    assert limits.friendly_error(Exception("что-то невиданное"), "ru") == t("generic_dl_failed", "ru")


def test_file_too_large_by_type():
    assert limits.friendly_error(limits.FileTooLargeError(), "ru") == t("err_too_large", "ru")


def test_heavy_downloads_are_capped():
    """Тяжёлых видео одновременно — не больше HEAVY_MAX: третий обязан ждать."""
    async def scenario():
        for _ in range(limits.HEAVY_MAX):
            await limits.acquire(limits.HEAVY)
        assert limits.queue_is_full(limits.HEAVY)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(limits.acquire(limits.HEAVY), 0.1)
        for _ in range(limits.HEAVY_MAX):
            await limits.release(limits.HEAVY)
        assert not limits.queue_is_full(limits.HEAVY)

    asyncio.run(scenario())


def test_light_tasks_get_more_slots_when_no_heavy_video():
    async def scenario():
        assert limits._limiter._cap(limits.LIGHT) == limits.LIGHT_BOOST
        await limits.acquire(limits.HEAVY)
        assert limits._limiter._cap(limits.LIGHT) == limits.LIGHT_BASE
        await limits.release(limits.HEAVY)

    asyncio.run(scenario())
