"""Оценка веса ролика до скачивания.

Потолок Telegram для бота — 2 ГБ. Раньше превышение выяснялось только ПОСЛЕ полной
загрузки: полчаса ожидания и гигабайты домашнего канала уходили впустую.
"""
import pytest

from bot.features.download.downloaders.ytdlp_wrapper import estimate_size

GB = 1024 ** 3


def _fmt(height=None, size=None, vcodec="avc1", acodec="none", approx=None):
    f = {"vcodec": vcodec, "acodec": acodec}
    if height:
        f["height"] = height
    if size:
        f["filesize"] = size
    if approx:
        f["filesize_approx"] = approx
    return f


def test_video_plus_audio_are_summed():
    info = {"formats": [
        _fmt(height=1080, size=3 * GB),
        _fmt(size=200 * 1024 * 1024, vcodec="none", acodec="mp4a"),
    ]}
    assert estimate_size(info, 1080) == 3 * GB + 200 * 1024 * 1024


def test_approximate_size_is_used_when_exact_is_missing():
    info = {"formats": [_fmt(height=720, approx=500 * 1024 * 1024)]}
    assert estimate_size(info, 720) == 500 * 1024 * 1024


def test_other_qualities_are_ignored():
    info = {"formats": [
        _fmt(height=2160, size=8 * GB),
        _fmt(height=720, size=700 * 1024 * 1024),
    ]}
    assert estimate_size(info, 720) == 700 * 1024 * 1024


def test_returns_none_when_nothing_to_measure():
    assert estimate_size({"formats": []}, 1080) is None
    assert estimate_size({"formats": [_fmt(height=1080)]}, 1080) is None
    assert estimate_size({}, 1080) is None


def test_four_k_movie_is_recognized_as_too_big():
    from bot.utils import limits
    info = {"formats": [
        _fmt(height=2160, size=5 * GB),
        _fmt(size=300 * 1024 * 1024, vcodec="none", acodec="mp4a"),
    ]}
    assert estimate_size(info, 2160) > limits.MAX_FILE_BYTES


def test_normal_video_passes():
    from bot.utils import limits
    info = {"formats": [_fmt(height=1080, size=300 * 1024 * 1024)]}
    assert estimate_size(info, 1080) < limits.MAX_FILE_BYTES
