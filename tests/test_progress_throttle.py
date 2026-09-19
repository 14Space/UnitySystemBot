"""Полоска загрузки не должна долбить Telegram правками."""
from bot.utils.progress_bar import ProgressThrottle, make_progress_bar


class _Clock:
    """Управляемое время: тест не должен ничего ждать по-настоящему."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _throttle(monkeypatch, interval=3.0):
    clock = _Clock()
    monkeypatch.setattr("bot.utils.progress_bar.time.monotonic", clock)
    return ProgressThrottle(min_interval=interval), clock


def test_first_value_always_goes_through(monkeypatch):
    th, _ = _throttle(monkeypatch)
    assert th.should_send(0)


def test_same_percent_is_skipped(monkeypatch):
    th, clock = _throttle(monkeypatch)
    th.should_send(10)
    clock.now += 60
    assert not th.should_send(10)


def test_updates_are_rate_limited(monkeypatch):
    th, clock = _throttle(monkeypatch)
    th.should_send(1)
    sent = 0
    for percent in range(2, 100):          # сыпем прогрессом без пауз, как yt-dlp
        if th.should_send(percent):
            sent += 1
    assert sent == 0                        # время не шло — ни одной лишней правки


def test_update_passes_after_the_interval(monkeypatch):
    th, clock = _throttle(monkeypatch)
    th.should_send(1)
    clock.now += 3.5
    assert th.should_send(40)


def test_hundred_percent_is_never_held_back(monkeypatch):
    th, clock = _throttle(monkeypatch)
    th.should_send(1)
    assert th.should_send(100)              # конец загрузки видно сразу


def test_bar_looks_right():
    assert make_progress_bar(60) == "Загрузка... ██████░░░░ 60%"
