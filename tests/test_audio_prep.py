"""Подготовка звука перед распознаванием.

Правило простое: обработка помогает, но не обязана удаваться. Сломался ffmpeg, кривой
файл — расшифровка всё равно должна пойти, просто по исходному файлу.
"""
import os

from bot.features.transcribe import audio_prep


def test_real_file_is_converted():
    path, ours = audio_prep.prepare("data/samples/whisper_probe.ogg")
    try:
        assert ours is True
        assert path.endswith(".wav") and os.path.getsize(path) > 0
    finally:
        audio_prep.cleanup(path, ours)
    assert not os.path.exists(path)


def test_original_is_never_deleted():
    """Чужой файл не наш — его удаляет тот, кто скачивал."""
    src = "data/samples/whisper_probe.ogg"
    audio_prep.cleanup(src, False)
    assert os.path.exists(src)


def test_missing_file_falls_back(tmp_path):
    missing = str(tmp_path / "нет-такого.ogg")
    assert audio_prep.prepare(missing) == (missing, False)


def test_broken_file_falls_back(tmp_path):
    """Кривой файл ffmpeg не переварит — работаем по исходному."""
    broken = tmp_path / "broken.ogg"
    broken.write_text("это не звук", encoding="utf-8")
    assert audio_prep.prepare(str(broken)) == (str(broken), False)


def test_switch_off(monkeypatch):
    monkeypatch.setattr(audio_prep, "STT_PREPROCESS", False)
    src = "data/samples/whisper_probe.ogg"
    assert audio_prep.prepare(src) == (src, False)
