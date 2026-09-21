"""Тормозящий провайдер ИИ не должен держать очередь.

21.09.2026 Gemini отвечал по 36-46с и отдавал 503. Groq рядом отвечает за полсекунды,
но ходили к нему только после того, как каждый спрашивающий отстоял таймаут.
"""
import pytest
import requests

from bot.features.ai import client


@pytest.fixture(autouse=True)
def _clean_rest():
    client._resting.clear()
    yield
    client._resting.clear()


def _providers(monkeypatch, gemini, groq, order=("gemini", "groq")):
    monkeypatch.setattr(client, "GEMINI_API_KEY", "ключ")
    monkeypatch.setattr(client, "GROQ_API_KEY", "ключ")
    monkeypatch.setattr(client, "_gemini", gemini)
    monkeypatch.setattr(client, "_groq", groq)
    monkeypatch.setattr(client, "AI_ORDER", order)


def test_timeout_falls_through_to_the_next_provider(monkeypatch):
    def slow(history, lang):
        raise requests.Timeout("не успел")

    _providers(monkeypatch, slow, lambda h, l: ("быстрый ответ", "ok"))
    assert client.ask([{"role": "user", "content": "привет"}]) == ("быстрый ответ", "ok")


def test_slow_provider_is_skipped_next_time(monkeypatch):
    calls = []

    def slow(history, lang):
        calls.append("gemini")
        raise requests.Timeout("не успел")

    def fast(history, lang):
        calls.append("groq")
        return "ответ", "ok"

    _providers(monkeypatch, slow, fast)
    q = [{"role": "user", "content": "привет"}]
    client.ask(q)
    client.ask(q)
    assert calls == ["gemini", "groq", "groq"]      # второй раз к тормозящему не пошли


def test_everyone_resting_is_still_asked(monkeypatch):
    """Отдыхают все — идём всё равно: медленный ответ лучше, чем никакого."""
    _providers(monkeypatch, lambda h, l: ("ответ", "ok"), lambda h, l: (None, "error"))
    client._rest("gemini", 600, "тест")
    client._rest("groq", 600, "тест")
    assert client.ask([{"role": "user", "content": "привет"}]) == ("ответ", "ok")


def test_success_wakes_the_provider_up(monkeypatch):
    _providers(monkeypatch, lambda h, l: ("ответ", "ok"), lambda h, l: (None, "error"))
    client._rest("gemini", 600, "тест")
    client._rest("groq", 600, "тест")     # иначе к отдыхающему вообще не пойдут
    client.ask([{"role": "user", "content": "привет"}])
    assert client._awake("gemini")


def test_no_keys_means_no_provider(monkeypatch):
    monkeypatch.setattr(client, "GEMINI_API_KEY", "")
    monkeypatch.setattr(client, "GROQ_API_KEY", "")
    assert client.ask([{"role": "user", "content": "привет"}]) == (None, "no_provider")


def test_order_comes_from_the_setting(monkeypatch):
    """Порядок провайдеров живёт в .env: Groq быстрее, но вкусы у всех разные."""
    calls = []

    def gemini(history, lang):
        calls.append("gemini")
        return "от gemini", "ok"

    def groq(history, lang):
        calls.append("groq")
        return "от groq", "ok"

    q = [{"role": "user", "content": "привет"}]
    _providers(monkeypatch, gemini, groq, order=("groq", "gemini"))
    assert client.ask(q) == ("от groq", "ok")
    _providers(monkeypatch, gemini, groq, order=("gemini", "groq"))
    assert client.ask(q) == ("от gemini", "ok")
    assert calls == ["groq", "gemini"]


def test_unknown_name_in_the_setting_is_skipped(monkeypatch):
    """Опечатка в AI_ORDER не должна оставлять бота вовсе без ИИ."""
    _providers(monkeypatch, lambda h, l: ("от gemini", "ok"),
               lambda h, l: ("от groq", "ok"), order=("грок", "gemini"))
    assert client.ask([{"role": "user", "content": "привет"}]) == ("от gemini", "ok")


def test_provider_without_a_key_is_not_asked(monkeypatch):
    _providers(monkeypatch, lambda h, l: ("от gemini", "ok"),
               lambda h, l: ("от groq", "ok"), order=("groq", "gemini"))
    monkeypatch.setattr(client, "GROQ_API_KEY", "")
    assert client.ask([{"role": "user", "content": "привет"}]) == ("от gemini", "ok")
