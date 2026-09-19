"""Узнаём ли мы площадку по ссылке.

Это первое, что делает бот с каждым сообщением: не узнал площадку — молча промолчал.
Ошибка здесь не падает с ошибкой, она выглядит как «бот не реагирует».
"""
import pytest

from bot.utils.platform_detector import detect_platform, Platform


@pytest.mark.parametrize("url, expected", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", Platform.YOUTUBE_VIDEO),
    ("https://youtu.be/dQw4w9WgXcQ", Platform.YOUTUBE_VIDEO),
    ("https://www.youtube.com/shorts/abc123", Platform.YOUTUBE_SHORTS),
    ("https://music.youtube.com/watch?v=abc", Platform.YT_MUSIC),
    ("https://soundcloud.com/artist/track", Platform.SOUNDCLOUD),
    ("https://soundcloud.com/artist/sets/album", Platform.SOUNDCLOUD_SET),
    ("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT", Platform.SPOTIFY),
    ("https://open.spotify.com/album/4m2880jivSbbyEGAKfITCa", Platform.SPOTIFY_COLLECTION),
    ("https://www.instagram.com/reel/DbW4UlRF4mc/", Platform.INSTAGRAM_REEL),
    ("https://www.instagram.com/p/DcGauVjJtS8/", Platform.INSTAGRAM_POST),
    ("https://www.tiktok.com/@user/video/7106594312292453675", Platform.TIKTOK),
    ("https://vt.tiktok.com/ZSVAe23Sh/", Platform.TIKTOK),
    ("https://www.pinterest.com/pin/99360735500167749/", Platform.PINTEREST),
    ("https://pin.it/2BrznYneL", Platform.PINTEREST),
    ("https://www.pornhub.com/view_video.php?viewkey=abc", Platform.PORNHUB),
    ("https://www.pornhub.com/shorties/abc", Platform.PORNHUB_SHORT),
    ("https://rezka.ag/films/fiction/45140-tron.html", Platform.HDREZKA),
    ("https://x.com/jack/status/20", Platform.TWITTER),
    ("https://twitter.com/jack/status/20", Platform.TWITTER),
])
def test_known_links(url, expected):
    assert detect_platform(url) == expected


@pytest.mark.parametrize("url", [
    "",
    "просто текст",
    "https://example.com/video",
    "https://x.com/jack",            # профиль без поста — качать нечего
])
def test_unknown_links(url):
    assert detect_platform(url) == Platform.UNKNOWN
