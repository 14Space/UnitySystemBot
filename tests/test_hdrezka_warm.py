"""Пропуск HDRezka обновляется заранее, а не тем, кто первым пришёл.

21.09.2026 «HDRezka сериал» занял 42.6с вместо 1.6с: анти-бот-головоломку решает
браузер на общем потоке Playwright (там же карточки X и фото Instagram), и в час
общей проверки она ждала очереди. Пока пропуск свежий, браузер в этом пути не нужен.
"""
import time

from bot.features.download.downloaders import hdrezka_gate as gate


def _reset(cookies=None, age=0.0):
    gate._cookies = cookies
    gate._cookies_ts = time.time() - age


def test_warm_replaces_the_pass(monkeypatch):
    _reset({"старый": "1"})
    monkeypatch.setattr(gate.pw_thread, "run_with_browser",
                        lambda fn, *a, **kw: {"новый": "2"})
    assert gate.warm() is True
    assert gate.get_cookies("https://rezka.ag/") == {"новый": "2"}


def test_failed_warm_keeps_the_old_pass(monkeypatch):
    """Не решилась головоломка — не беда: прежний пропуск ещё годен."""
    _reset({"старый": "1"})

    def boom(fn, *a, **kw):
        raise RuntimeError("браузер занят")

    monkeypatch.setattr(gate.pw_thread, "run_with_browser", boom)
    assert gate.warm() is False
    assert gate.get_cookies("https://rezka.ag/") == {"старый": "1"}


def test_warm_resets_the_clock(monkeypatch):
    """После обновления пропуск снова свежий — никто не пойдёт решать заново."""
    _reset({"старый": "1"}, age=gate._TTL + 10)      # уже протух
    monkeypatch.setattr(gate.pw_thread, "run_with_browser",
                        lambda fn, *a, **kw: {"новый": "2"})
    gate.warm()

    calls = []
    monkeypatch.setattr(gate.pw_thread, "run_with_browser",
                        lambda fn, *a, **kw: calls.append(1) or {"лишний": "3"})
    gate.get_cookies("https://rezka.ag/")
    assert calls == []                               # браузер не понадобился


def test_warm_interval_leaves_a_margin():
    assert 60 <= gate.warm_interval() < gate._TTL
