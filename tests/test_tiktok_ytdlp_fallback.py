"""30.09.2026: tikwm отвечал 403 всем, lovetik не находил ролик – видео брал yt-dlp."""
from bot.features.download.downloaders import tiktok


def test_video_falls_back_to_ytdlp(monkeypatch):
    monkeypatch.setattr(tiktok, "_api_call_retry", lambda *a, **k: {"code": -1, "msg": "403"})
    monkeypatch.setattr(tiktok, "_api_call", lambda *a, **k: {"code": -1, "msg": "403"})
    monkeypatch.setattr(tiktok, "_resolve_short", lambda url: url)
    monkeypatch.setattr(tiktok, "_fetch_backup", lambda url: None)
    monkeypatch.setattr(tiktok, "_fetch_ytdlp",
                        lambda url: {"id": "1", "kind": "video", "data": {"id": "1", "ytdlp": url}})
    info = tiktok._fetch_tiktok_api("https://www.tiktok.com/@a/video/1")
    assert info["data"]["ytdlp"] == "https://www.tiktok.com/@a/video/1"


def test_no_watermarked_format():
    """«download» у yt-dlp – вариант с водяным знаком."""
    assert "format_id!=download" in tiktok._YTDLP_FORMAT
