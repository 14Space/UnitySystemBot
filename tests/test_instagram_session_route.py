"""Куки Instagram не должны уезжать с серверного адреса — даже через браузер.

Правило старое: свою сессию, увиденную из дата-центра, Instagram считает угоном и
закрывает вход. Для обычных запросов оно соблюдалось, а вот рендер страницы в
браузере получал куки и шёл напрямую — и 23.09.2026 сессия умерла снова.
"""
from bot.features.download.downloaders import instagram


class FakeContext:
    def __init__(self, **options):
        self.options = options
        self.cookies = None
        self.closed = False

    def add_cookies(self, cookies):
        self.cookies = cookies

    def new_page(self):
        raise RuntimeError("дальше страницы не идём — проверяем только маршрут")

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.last = None

    def new_context(self, **options):
        self.last = FakeContext(**options)
        return self.last


def _render(monkeypatch, proxy, cookies):
    monkeypatch.setattr(instagram, "INSTAGRAM_PROXY", proxy)
    monkeypatch.setattr(instagram, "_pw_cookies", lambda: cookies)
    browser = FakeBrowser()
    try:
        instagram._embed_image_src(browser, "CODE")
    except RuntimeError:
        pass                      # ожидаемо: до страницы дело не доходит
    return browser.last


def test_session_goes_through_home(monkeypatch):
    ctx = _render(monkeypatch, "socks5://10.8.0.2:1080", [{"name": "sessionid"}])
    assert ctx.options.get("proxy") == {"server": "socks5://10.8.0.2:1080"}
    assert ctx.cookies == [{"name": "sessionid"}]
    assert ctx.closed


def test_without_tunnel_we_go_as_a_guest(monkeypatch):
    """Нет туннеля — идём без кук: потерянный кадр дешевле потерянного входа."""
    ctx = _render(monkeypatch, "", [{"name": "sessionid"}])
    assert "proxy" not in ctx.options
    assert ctx.cookies is None


def test_without_cookies_no_proxy_is_needed(monkeypatch):
    ctx = _render(monkeypatch, "socks5://10.8.0.2:1080", [])
    assert "proxy" not in ctx.options
    assert ctx.cookies is None
