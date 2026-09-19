"""Место на диске и чистка памяти от старых переходов."""
import time

import pytest

from bot.utils import limits


def test_enough_space_passes(tmp_path):
    limits.check_disk_space(str(tmp_path), need=1)


def test_no_space_raises(tmp_path):
    with pytest.raises(limits.NoDiskSpaceError):
        limits.check_disk_space(str(tmp_path), need=10 ** 18)


def test_message_is_human(tmp_path):
    from bot.utils.i18n import t
    try:
        limits.check_disk_space(str(tmp_path), need=10 ** 18)
    except limits.NoDiskSpaceError as e:
        assert limits.friendly_error(e, "ru") == t("err_no_space", "ru")


def test_ffmpeg_errno_28_is_recognized():
    """ffmpeg на забитом диске падает с «errno 28» — это та же беда, не «сломалась ссылка»."""
    from bot.utils.i18n import t
    assert limits.friendly_error(Exception("OSError: [Errno 28] No space left on device"),
                                 "ru") == t("err_no_space", "ru")


def test_unknown_path_does_not_crash():
    assert limits.free_space("/такого/пути/нет") == 0
    limits.check_disk_space("/такого/пути/нет")      # узнать не вышло — не мешаем работать


def test_deeplinks_do_not_grow_forever():
    """Словарь переходов из inline раньше только рос — по записи на каждый переход."""
    from bot.features.common import start

    start._recent_deeplinks.clear()
    now = time.monotonic()
    for i in range(500):
        start._recent_deeplinks[f"old{i}"] = now - 60      # давно просроченные
    start._forget_old_deeplinks(now)
    assert start._recent_deeplinks == {}


def test_deeplinks_keep_fresh_entries():
    from bot.features.common import start

    start._recent_deeplinks.clear()
    now = time.monotonic()
    start._recent_deeplinks["свежий"] = now
    start._recent_deeplinks["старый"] = now - 60
    start._forget_old_deeplinks(now)
    assert list(start._recent_deeplinks) == ["свежий"]
