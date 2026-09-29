"""Groq разобрал начало, а конец голосового заменил выдумкой (27.09.2026)."""
from bot.features.transcribe.transcriber import groq_stt


def _seg(start, end, text):
    return {"start": start, "end": end, "text": text}


def test_long_segment_with_few_words_is_lost_speech():
    """17 секунд речи превратились в «Ссылка в описании.» – живой записи так не бывает."""
    data = {"segments": [
        _seg(0, 13, " Короче, вот смотри, я писал уже что-то по типу калькулятор, но не "
                    "калькулятор, потому что для него нужно знать, что такое if и else."),
        _seg(13, 21, " И короче, я написал код, он работал как калькулятор."),
        _seg(21, 38, " Ссылка в описании."),
    ]}
    assert groq_stt._lost_speech(data)


def test_video_caption_phantom_is_lost_speech():
    data = {"segments": [_seg(0, 8, " то есть ну он же Субтитры сделал DimaTorzok")]}
    assert groq_stt._lost_speech(data)


def test_prompt_echo_is_lost_speech(monkeypatch):
    monkeypatch.setattr(groq_stt, "GROQ_STT_PROMPT", "Ссылки не обрабатывает, апнуть серебро")
    data = {"text": "код калькулятора? Ссылки не обрабатывает, апнуть серебро",
            "segments": [_seg(0, 3, " код калькулятора? Ссылки не обрабатывает, апнуть серебро")]}
    assert groq_stt._lost_speech(data)


def test_normal_speech_passes():
    data = {"text": "да", "segments": [
        _seg(0, 8, " короче вот смотри я писал уже что-то по типу калькулятор но не калькулятор"),
        _seg(8, 9.5, " Да."),                   # короткий ответ – не повод для тревоги
    ]}
    assert groq_stt._lost_speech(data) == ""
