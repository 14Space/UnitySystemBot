"""Пульс: отметка о том, что бот жив."""
import time

from bot.utils import heartbeat


def test_touch_and_age(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "PATH", str(tmp_path / "beat"))
    heartbeat.touch()
    seconds = heartbeat.age()
    assert seconds is not None and seconds < 5


def test_age_is_none_without_file(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "PATH", str(tmp_path / "нет-такого"))
    assert heartbeat.age() is None


def test_age_is_none_on_garbage(tmp_path, monkeypatch):
    """Обрубок файла (например, после падения посреди записи) не должен ломать сторожа."""
    path = tmp_path / "beat"
    path.write_text("не число")
    monkeypatch.setattr(heartbeat, "PATH", str(path))
    assert heartbeat.age() is None


def test_stale_beat_is_detected(tmp_path, monkeypatch):
    path = tmp_path / "beat"
    path.write_text(str(int(time.time()) - heartbeat.STALE_AFTER - 10))
    monkeypatch.setattr(heartbeat, "PATH", str(path))
    assert heartbeat.age() > heartbeat.STALE_AFTER
