from enum import Enum
from urllib.parse import urlparse


class Platform(Enum):
    YOUTUBE_VIDEO = "youtube_video"
    YOUTUBE_SHORTS = "youtube_shorts"
    YT_MUSIC = "yt_music"
    SOUNDCLOUD = "soundcloud"
    SOUNDCLOUD_SET = "soundcloud_set"  # альбом/плейлист SoundCloud (/sets/)
    SPOTIFY = "spotify"
    SPOTIFY_COLLECTION = "spotify_collection"  # альбом или плейлист
    INSTAGRAM_REEL = "instagram_reel"  # короткое видео (как Shorts)
    INSTAGRAM_POST = "instagram_post"  # пост /p/: фото, видео или карусель
    TIKTOK = "tiktok"
    PINTEREST = "pinterest"  # пин: фото или видео
    PORNHUB = "pornhub"
    PORNHUB_SHORT = "pornhub_short"  # shorties — короткие вертикальные видео
    HDREZKA = "hdrezka"  # фильмы и сериалы
    UNKNOWN = "unknown"


def detect_platform(url: str) -> Platform:
    """Определяет платформу по ссылке"""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().replace("www.", "")
        path = parsed.path.lower()

        # YouTube Music — отдельный домен, но качаем как аудио
        if domain == "music.youtube.com":
            return Platform.YT_MUSIC

        if domain in ("youtube.com", "youtu.be", "m.youtube.com"):
            if "/shorts/" in path:
                return Platform.YOUTUBE_SHORTS
            return Platform.YOUTUBE_VIDEO

        # SoundCloud: soundcloud.com и короткие ссылки on.soundcloud.com
        if domain in ("soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"):
            if "/sets/" in path:
                return Platform.SOUNDCLOUD_SET
            return Platform.SOUNDCLOUD

        # HDRezka (rezka.ag и зеркала: hdrezka.me и т.п.)
        if "rezka" in domain:
            return Platform.HDREZKA

        # PornHub
        if "pornhub.com" in domain:
            if "/shorties/" in path:
                return Platform.PORNHUB_SHORT
            return Platform.PORNHUB

        # TikTok (включая короткие ссылки vm./vt.)
        if "tiktok.com" in domain:
            return Platform.TIKTOK

        # Pinterest (много доменов: .com/.ca/.co.uk + короткие pin.it)
        if "pinterest" in domain or domain == "pin.it":
            return Platform.PINTEREST

        # Instagram
        if domain in ("instagram.com", "m.instagram.com", "ddinstagram.com"):
            if "/reel/" in path or "/reels/" in path:
                return Platform.INSTAGRAM_REEL
            if "/p/" in path or "/tv/" in path:
                return Platform.INSTAGRAM_POST

        # Spotify: качаем через поиск трека на YouTube (напрямую DRM не даёт)
        if domain in ("open.spotify.com", "spotify.com"):
            if "/album/" in path or "/playlist/" in path:
                return Platform.SPOTIFY_COLLECTION
            return Platform.SPOTIFY

    except Exception:
        pass

    return Platform.UNKNOWN


def is_supported(url: str) -> bool:
    return detect_platform(url) != Platform.UNKNOWN
