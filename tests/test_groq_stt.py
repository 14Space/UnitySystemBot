"""Как мы зовём облачную расшифровку.

Модель та же, что крутилась локально (whisper-large-v3), но у облака нет ни перебора
вариантов (beam search), ни фильтра тишины — поэтому важно хотя бы то, чем мы можем
управлять: стиль речи, нулевая температура и перечитывание при сомнениях.
"""
import pytest

from bot.features.transcribe.transcriber import groq_stt


def _data(text="привет", lang="russian", logprob=-0.2, no_speech=0.01):
    return {"text": text, "language": lang,
            "segments": [{"avg_logprob": logprob, "no_speech_prob": no_speech}]}


def test_confidence_is_averaged():
    data = {"segments": [{"avg_logprob": -0.2}, {"avg_logprob": -0.6}]}
    assert groq_stt._confidence(data) == pytest.approx(-0.4)


def test_confidence_without_segments():
    assert groq_stt._confidence({}) == 0.0


def test_language_is_not_forced_by_default():
    """Бот многоязычный: жёсткий русский превратил бы украинскую речь в кашу."""
    from bot.config import GROQ_STT_LANGUAGE
    assert GROQ_STT_LANGUAGE == ""


def test_confident_answer_is_taken_as_is(monkeypatch):
    calls = []

    def fake_ask(path, language=""):
        calls.append(language)
        return _data("уверенный разбор")

    monkeypatch.setattr(groq_stt, "_ask", fake_ask)
    assert groq_stt.transcribe("x.ogg") == "уверенный разбор"
    assert calls == [""]                      # второго запроса не было


def test_unsure_answer_is_reread_in_primary_language(monkeypatch):
    calls = []

    def fake_ask(path, language=""):
        calls.append(language)
        if not language:
            return _data("мутный разбор", logprob=-0.9)
        return _data("чёткий разбор", logprob=-0.2)

    monkeypatch.setattr(groq_stt, "_ask", fake_ask)
    assert groq_stt.transcribe("x.ogg") == "чёткий разбор"
    assert calls == ["", "ru"]                # перечитали на основном языке


def test_reread_is_rejected_when_it_is_worse(monkeypatch):
    """Иногда первый разбор — лучшее, что можно вытащить из записи."""
    def fake_ask(path, language=""):
        if not language:
            return _data("первый", logprob=-0.9)
        return _data("хуже", logprob=-1.5)

    monkeypatch.setattr(groq_stt, "_ask", fake_ask)
    assert groq_stt.transcribe("x.ogg") == "первый"


def test_foreign_language_triggers_a_reread(monkeypatch):
    calls = []

    def fake_ask(path, language=""):
        calls.append(language)
        if not language:
            return _data("krokodil", lang="croatian")
        return _data("нормальный русский")

    monkeypatch.setattr(groq_stt, "_ask", fake_ask)
    assert groq_stt.transcribe("x.ogg") == "нормальный русский"
    assert calls == ["", "ru"]


def test_silence_still_returns_nothing(monkeypatch):
    monkeypatch.setattr(groq_stt, "_ask",
                        lambda path, language="": _data("you", no_speech=0.95))
    assert groq_stt.transcribe("x.ogg") == ""
