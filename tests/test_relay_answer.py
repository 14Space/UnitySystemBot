"""Что чужой бот-расшифровщик прислал: расшифровку или отказ.

Отказ он присылает обычным сообщением, ничем не отличающимся от результата. Раньше
мы показывали такой ответ человеку как его же слова — 20.09.2026 на видео-кружок
бот процитировал «Не удалось ничего распознать. Вы прислали аудио без речи.».
"""
import pytest

from bot.features.transcribe.transcriber import relay_stt


@pytest.mark.parametrize("answer", [
    "Не удалось ничего распознать. Вы прислали аудио без речи.",
    "В аудио нет речи",
    "Тишина",
    "No speech detected",
])
def test_no_speech_means_silence(answer):
    assert relay_stt._as_result(answer) == ""


@pytest.mark.parametrize("answer", [
    "Произошла ошибка, попробуйте позже",
    "Файл слишком большой",
    "Превышен дневной лимит",
    "Формат не поддерживается",
])
def test_refusal_is_a_failure(answer):
    with pytest.raises(RuntimeError):
        relay_stt._as_result(answer)


@pytest.mark.parametrize("answer", [
    "Привет, я сегодня не успеваю, перенесём на завтра",
    "Тишина в зале была полная, все ждали выхода артиста",
    "Слушай, у меня ошибка в отчёте, надо пересчитать",
    "Тишина, распознавать нечего",
])
def test_real_transcription_passes(answer):
    """Слова-приметы звучат и в живой речи. Отказ узнаём только там, где заготовка
    бота и стоит — в начале короткого сообщения; «тишина» — лишь как весь ответ."""
    assert relay_stt._as_result(answer) == answer


def test_long_answer_is_never_a_verdict():
    """Длинный текст — это расшифровка, даже если в ней есть «нет речи»."""
    long = "в записи нет речи, " + "дальше человек говорит без остановки, " * 10
    assert relay_stt._as_result(long) == long.strip()


def test_voice_conversion_keeps_ogg_as_is():
    assert relay_stt._as_voice("data/samples/whisper_probe.ogg") == (
        "data/samples/whisper_probe.ogg", False)


def test_voice_conversion_falls_back_on_broken_file(tmp_path):
    """Не собралось голосовое — шлём что есть, хуже прежнего не будет."""
    broken = tmp_path / "broken.wav"
    broken.write_text("это не звук", encoding="utf-8")
    assert relay_stt._as_voice(str(broken)) == (str(broken), False)


def test_real_wav_becomes_voice():
    from bot.features.transcribe import audio_prep

    prepared, ours = audio_prep.prepare("data/samples/whisper_probe.ogg")
    try:
        assert prepared.endswith(".wav")          # именно это и уходило чужому боту
        voice, mine = relay_stt._as_voice(prepared)
        try:
            assert mine is True and voice.endswith(".ogg")
        finally:
            relay_stt._drop(voice)
    finally:
        audio_prep.cleanup(prepared, ours)
