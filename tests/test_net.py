"""Одна сетевая логика на весь проект (bot/utils/net.py): чей адрес и через что идти.

Раньше прокси была написана четырежды, с разными переменными окружения, а проверка
«ссылка ведёт на площадку» была аккуратной в одном месте и подстрокой в другом.
"""
import pytest

from bot.utils import net


@pytest.mark.parametrize("url, ok", [
    ("https://www.youtube.com/watch?v=x", True),
    ("https://youtu.be/x", True),
    ("https://evil.com/?youtube.com", False),        # наше слово в параметрах
    ("https://youtube.com.evil.com/", False),        # наш домен внутри чужого
    ("https://youtube.com@evil.com/", False),        # логин в адресе
    ("https://youtube.com:8080/", False),            # нестандартный порт
    ("", False),
])
def test_url_on_checks_the_host_not_the_text(url, ok):
    assert net.url_on(url, "youtube.com", "youtu.be") is ok


def test_youtube_cookies_do_not_leave_for_a_lookalike(monkeypatch, tmp_path):
    """S2: в yt-dlp проверка шла подстрокой, и ссылка «evil.com/?youtube.com»
    получала прокси и файл кук YouTube."""
    from bot.features.download.downloaders import ytdlp_wrapper as y

    cookies = tmp_path / "youtube_cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setattr(y, "YOUTUBE_COOKIES", str(cookies))
    monkeypatch.setattr(net, "PROXY_URL", "socks5://дом:1080")

    assert not y._needs_proxy("https://evil.com/?youtube.com")
    assert y._cookie_opts("https://evil.com/?youtube.com", {"proxy": "socks5://дом:1080"}) == {}


def _recorder(fail_direct: bool):
    calls = []

    def op(proxy):
        calls.append(proxy)
        if fail_direct and not proxy:
            raise RuntimeError("блок по адресу")
        return proxy or "напрямую"
    return op, calls


def test_no_proxy_means_one_direct_call(monkeypatch):
    op, calls = _recorder(fail_direct=False)
    assert net.with_proxy(op, "") == "напрямую"
    assert calls == [""]


def test_direct_first_then_proxy(monkeypatch):
    monkeypatch.setattr(net, "PROXY_FIRST", False)
    op, calls = _recorder(fail_direct=True)
    assert net.with_proxy(op, "socks5://дом") == "socks5://дом"
    assert calls == ["", "socks5://дом"]


def test_proxy_first_skips_the_doomed_direct_try(monkeypatch):
    monkeypatch.setattr(net, "PROXY_FIRST", True)
    op, calls = _recorder(fail_direct=True)
    net.with_proxy(op, "socks5://дом")
    assert calls == ["socks5://дом"]


def test_pornhub_always_goes_through_the_proxy(monkeypatch):
    """Блок по стране: прямая попытка обречена, даже без PROXY_FIRST."""
    from bot.features.download.downloaders import ytdlp_wrapper as y

    monkeypatch.setattr(net, "PROXY_FIRST", False)
    monkeypatch.setattr(net, "PROXY_URL", "socks5://дом:1080")
    seen = []
    y.via_proxy("https://www.pornhub.com/view_video.php?viewkey=1",
                lambda opts: seen.append(opts) or "ok")
    assert [o.get("proxy") for o in seen] == ["socks5://дом:1080"]


def test_fetch_bytes_has_a_size_limit(monkeypatch):
    """Обложка по ссылке: вместо картинки может приехать что угодно и сколько угодно."""
    from bot.utils.limits import FileTooLargeError

    class Resp:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            for _ in range(10):
                yield b"x" * 1024

    class Session:
        def get(self, *a, **k):
            return Resp()

    monkeypatch.setattr(net, "session", lambda: Session())
    assert len(net.fetch_bytes("https://x/", max_bytes=20 * 1024)) == 10 * 1024
    with pytest.raises(FileTooLargeError):
        net.fetch_bytes("https://x/", max_bytes=4 * 1024)
