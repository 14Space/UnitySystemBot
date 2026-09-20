"""Ссылка вынимается из текста сообщения.

Люди почти никогда не присылают голую ссылку — рядом идёт подпись. Раньше как адрес
брали ВЕСЬ текст: «https://vt.tiktok.com/ZSq7wBhhs\n\nЛя шо коты умеют ваще» уезжало
на площадку целиком, и та отвечала «Url parsing is failed». Выглядело как поломка
TikTok, хотя ссылка была нормальная.
"""
import pytest

from bot.utils.platform_detector import extract_url, detect_platform, Platform


@pytest.mark.parametrize("text, expected", [
    ("https://vt.tiktok.com/ZSq7wBhhs\n\nЛя шо коты умеют ваще",
     "https://vt.tiktok.com/ZSq7wBhhs"),
    ("смотри что нашёл https://vt.tiktok.com/ZSq7wBhhs",
     "https://vt.tiktok.com/ZSq7wBhhs"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc вот это послушай",
     "https://youtu.be/dQw4w9WgXcQ?si=abc"),
    ("это видео https://vt.tiktok.com/ZSq7wBhhs.", "https://vt.tiktok.com/ZSq7wBhhs"),
    ("вот: https://vt.tiktok.com/ZSq7wBhhs!", "https://vt.tiktok.com/ZSq7wBhhs"),
    ("(https://x.com/jack/status/20)", "https://x.com/jack/status/20"),
    ("http://example.com/x", "http://example.com/x"),
])
def test_link_is_extracted(text, expected):
    assert extract_url(text) == expected


@pytest.mark.parametrize("text", ["просто текст", "", "   ", "www.tiktok.com/x"])
def test_no_link_found(text):
    assert extract_url(text) == ""


def test_first_link_wins():
    text = "https://vt.tiktok.com/A и ещё https://youtu.be/B"
    assert extract_url(text) == "https://vt.tiktok.com/A"


def test_platform_is_detected_after_extraction():
    """Главный случай из жизни: ссылка с подписью должна доехать до TikTok целой."""
    url = extract_url("https://vt.tiktok.com/ZSq7wBhhs\n\nЛя шо коты умеют ваще")
    assert detect_platform(url) == Platform.TIKTOK
    assert "\n" not in url and " " not in url


def test_message_with_link_counts_as_request():
    """Ссылка с подписью — обращение к боту, а не чужая болтовня."""
    from bot.middlewares.routing import is_request

    class _Msg:
        text = "https://vt.tiktok.com/ZSq7wBhhs\n\nЛя шо коты умеют ваще"
        voice = video_note = caption = None

    assert is_request(_Msg())
