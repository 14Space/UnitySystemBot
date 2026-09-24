"""Повторный аудит 24.09.2026: то, что после первого аудита осталось открытым.

Главное – подделка Pinterest. Проверка принимала любой адрес вида
pinterest.<что-угодно>, а yt-dlp с включённым «generic» по такой странице скачивал
любой адрес, в том числе внутренней сети, и писал его в файл с именем от площадки
(«../» выводило запись из папки загрузок). Всё это было проверено на деле.
"""
import asyncio
import os
import sys

import pytest

from bot.utils.platform_detector import Platform, detect_platform


@pytest.mark.parametrize("url", [
    "https://pinterest.evil.ru/pin/1",
    "http://169.254.169.254.pinterest.com.evil.io/",
    "https://www.pinterest.a.bc/pin/1",
    "https://pinterest.com@evil.com/pin/1",
])
def test_fake_pinterest_is_not_pinterest(url):
    assert detect_platform(url) == Platform.UNKNOWN


@pytest.mark.parametrize("url", [
    "https://www.pinterest.com/pin/123/",
    "https://ru.pinterest.com/pin/123/",
    "https://pinterest.co.uk/pin/123/",
    "https://pin.it/2BrznYneL",
])
def test_real_pinterest_still_works(url):
    assert detect_platform(url) == Platform.PINTEREST


def test_ytdlp_never_uses_the_generic_extractor():
    import yt_dlp
    from bot.features.download.downloaders.ytdlp_wrapper import BASE_OPTS

    ydl = yt_dlp.YoutubeDL({**BASE_OPTS, "quiet": True})
    assert "Generic" not in ydl._ies
    # а свои площадки на месте
    for name in ("Youtube", "YoutubeSearch", "Soundcloud", "SoundcloudSearch",
                 "PornHub", "Pinterest", "Instagram", "TikTok"):
        assert name in ydl._ies, name


def test_media_id_cannot_leave_the_downloads_folder():
    from bot.features.download.downloaders.ytdlp_wrapper import _safe_id

    assert _safe_id("../../pwn") == "media"
    assert _safe_id(r"..\evil") == "media"
    assert _safe_id("7529524373780323") == "7529524373780323"


def test_redirects_are_checked_before_they_are_followed(monkeypatch):
    """Короткие ссылки разворачиваем сами: запрос на чужой адрес не должен уйти вовсе."""
    from bot.utils import net

    visited = []

    class _Resp:
        def __init__(self, loc):
            self.headers = {"Location": loc} if loc else {}

        def close(self):
            pass

    class _Session:
        def get(self, url, **kw):
            visited.append(url)
            assert kw.get("allow_redirects") is False
            return _Resp({"https://pin.it/x": "http://10.0.0.1/admin"}.get(url))

    monkeypatch.setattr(net, "session", lambda: _Session())
    with pytest.raises(ValueError):
        net.follow_redirects("https://pin.it/x", "pin.it", "pinterest.com")
    assert visited == ["https://pin.it/x"]


def test_home_socks_refuses_local_and_private_addresses():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    try:
        import socks_server as s
    finally:
        sys.path.pop(0)
    for ip in ("127.0.0.1", "192.168.1.1", "10.8.0.1", "169.254.169.254", "::1",
               "::ffff:192.168.0.1", "100.64.0.1", "0.0.0.0"):
        assert not s._allowed_ip(ip), ip
    assert s._allowed_ip("8.8.8.8")

    async def resolve():
        return await s._resolve_public("localhost", 443)

    with pytest.raises(PermissionError):
        asyncio.run(resolve())


def test_tweet_card_files_do_not_collide(tmp_path, monkeypatch):
    from bot.features.download.renderer import tweet_card

    monkeypatch.setattr(tweet_card, "DOWNLOADS_DIR", str(tmp_path))
    seen = []
    monkeypatch.setattr(tweet_card, "_render_embed",
                        lambda browser, tweet, out: seen.append(out) or out)
    tweet_card._render_card(None, {"id": "1"})
    tweet_card._render_card(None, {"id": "1"})
    assert len(set(seen)) == 2


def test_health_alert_does_not_carry_the_bot_token():
    from bot.features.common.healthcheck import format_alert
    from bot.utils.secrets_filter import mask

    token = "1234567890:AAEabcdefghijklmnopqrstuvwxyz0123456"
    detail = mask(f"ConnectionError: HTTPConnectionPool(host='telegram-bot-api', "
                  f"port=8081): Max retries exceeded with url: /bot{token}/x")[:140]
    alert = format_alert([{"name": "send_path", "state": "fail", "detail": detail,
                           "platform": None}], "ru")
    assert "AAEabcdefghij" not in alert


def test_every_ytdlp_entry_point_unrolls_short_links():
    """24.09.2026, сразу после раскатки: разворот on.soundcloud.com стоял только в
    обработчике сообщений, и проверка «SoundCloud трек» с короткой ссылкой покраснела –
    yt-dlp без «generic» её не узнаёт. Разворачивать обязана КАЖДАЯ точка входа."""
    import inspect
    from bot.features.download.downloaders import ytdlp_wrapper as w

    for name in ("download_probe", "get_video_info", "download_video", "get_soundcloud_set",
                 "download_audio", "download_media", "download_shorts"):
        assert "resolve_short(url)" in inspect.getsource(getattr(w, name)), name


def test_resolve_short_leaves_full_links_alone(monkeypatch):
    from bot.features.download.downloaders import ytdlp_wrapper as w

    monkeypatch.setattr(w.net, "follow_redirects",
                        lambda url, *d: "https://soundcloud.com/a/b")
    assert w.resolve_short("https://on.soundcloud.com/XyZ") == "https://soundcloud.com/a/b"
    assert w.resolve_short("https://soundcloud.com/a/b") == "https://soundcloud.com/a/b"
    assert w.resolve_short("ytsearch1:x") == "ytsearch1:x"
