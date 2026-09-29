"""ВРЕМЕННО – до мини-ПК: удаляется вместе с bot/utils/home_tunnel.py."""
import asyncio

import pytest

from bot.utils import home_tunnel, net


@pytest.fixture
def tunnel(monkeypatch):
    monkeypatch.setattr(net, "PROXY_URL", "socks5://10.8.0.2:1080")
    monkeypatch.setattr(net, "INSTAGRAM_PROXY", "")
    monkeypatch.setattr(net, "PROXY_FIRST", True)
    monkeypatch.setattr(home_tunnel, "_down", None)

    def set_alive(alive: bool):
        monkeypatch.setattr(home_tunnel, "_probe_sync", lambda proxy: alive)
        asyncio.run(home_tunnel.check_now())
    return set_alive


def test_down_tunnel_goes_direct(tunnel):
    """Дом выключен – не ждём мёртвый прокси, а сразу идём напрямую."""
    tunnel(False)
    assert net.with_proxy(lambda proxy: proxy, net.proxy_for()) == ""
    tunnel(True)
    assert net.with_proxy(lambda proxy: proxy, net.proxy_for()) == "socks5://10.8.0.2:1080"


def test_unknown_state_behaves_as_before(tunnel):
    """До первой проверки ведём себя как раньше – через дом."""
    assert not home_tunnel.is_down()


def test_youtube_cookies_never_go_direct(tunnel):
    """Прямая попытка из-за выключенного дома не должна унести куки YouTube."""
    from bot.features.download.downloaders import ytdlp_wrapper

    tunnel(False)
    assert ytdlp_wrapper._cookie_opts("https://www.youtube.com/watch?v=x", {}) == {}


def test_no_admin_alert_while_down(tunnel, monkeypatch):
    from bot.features.common import alerts

    sent = []
    monkeypatch.setattr(alerts, "_bot", object())
    monkeypatch.setattr(alerts, "_admin_id", 1)
    monkeypatch.setattr(alerts, "_send", lambda text: sent.append(text))
    tunnel(False)

    async def fail():
        alerts.note_failure(RuntimeError("туннель"))
    asyncio.run(fail())
    assert sent == []


def test_failed_download_is_silent_while_down(tunnel, monkeypatch):
    """Дом выключен и загрузка упала – человеку ничего не пишем."""
    from contextlib import asynccontextmanager
    from bot.features.download import job

    @asynccontextmanager
    async def no_slot(*a, **k):
        yield
    monkeypatch.setattr(job, "slot", no_slot)
    tunnel(False)
    told = []

    async def work(paths):
        raise RuntimeError("не скачалось")

    async def on_error(e):
        told.append(e)

    asyncio.run(job.produce(cache=None, work=work, on_error=on_error))
    assert told == []
