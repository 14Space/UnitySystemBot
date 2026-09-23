import re
from enum import Enum
from urllib.parse import urlparse, parse_qsl

from bot.config import HDREZKA_DOMAINS
from bot.utils.net import host_of, on_domain

_TW_STATUS_RE = re.compile(r"/status/(\d+)")


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
    TWITTER = "twitter"  # X (Twitter): фото/видео/gif/текст одного поста
    UNKNOWN = "unknown"


# Ссылка внутри обычного сообщения. Люди почти никогда не присылают «голую» ссылку:
# рядом идёт подпись («вот, глянь»), пересланный текст, эмодзи. Кириллицу и пробелы в
# сам адрес не пускаем — на них ссылка и заканчивается.
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
# Хвостовая пунктуация: «смотри https://vt.tiktok.com/ABC.» — точка в конце фразы,
# а не часть адреса. Скобку закрывающую убираем только если её не открывали внутри.
_TRAILING = ".,;:!?»\"'）)]}"


def extract_url(text: str) -> str:
    """Первая ссылка из текста сообщения («» — если её там нет).

    Зачем: раньше как ссылку брали ВЕСЬ текст сообщения. Стоило человеку написать
    «https://vt.tiktok.com/ZSq7wBhhs\n\nЛя шо коты умеют» — и площадке уходила вся
    эта строка целиком, а она отвечала «Url parsing is failed». Выглядело как
    поломка TikTok, хотя ссылка была совершенно нормальная.
    """
    m = _URL_RE.search(text or "")
    if not m:
        return ""
    url = m.group(0)
    while url and url[-1] in _TRAILING:
        if url[-1] == ")" and url.count("(") > url.count(")"):
            break            # скобка — часть адреса (бывает в википедии и т.п.)
        url = url[:-1]
    return url



# --- Хозяин ссылки ---------------------------------------------------------
# Сверяем хозяина адреса целиком, а не подстроку: правило и его причины – в
# bot/utils/net.py (host_of), им же пользуются загрузчики.


# Зеркала HDRezka: раньше ловились подстрокой «rezka», то есть подходил любой домен,
# где эти буквы просто встречаются. Список можно дополнить через .env, не пересобирая
# образ: площадка меняет зеркала чаще, чем мы выпускаем версии.
_HDREZKA_DOMAINS = HDREZKA_DOMAINS
# Pinterest живёт на десятке национальных доменов (.com/.ru/.ca/.co.uk…), поэтому здесь
# проверяем форму хозяина, а не список: «pinterest.<что-то>» и короткий pin.it.
_PINTEREST_RE = re.compile(r"^(?:.+\.)?pinterest\.[a-z][a-z.]{1,7}$")


def detect_platform(url: str) -> Platform:
    """Определяет платформу по ссылке"""
    try:
        parsed = urlparse(url)
        host = host_of(parsed)
        if not host:
            return Platform.UNKNOWN
        domain = host[4:] if host.startswith("www.") else host
        path = parsed.path.lower()

        # YouTube Music — отдельный домен, но качаем как аудио
        if on_domain(host, "music.youtube.com"):
            return Platform.YT_MUSIC

        if on_domain(domain, "youtube.com", "youtu.be"):
            if "/shorts/" in path:
                return Platform.YOUTUBE_SHORTS
            return Platform.YOUTUBE_VIDEO

        # SoundCloud: soundcloud.com и короткие ссылки on.soundcloud.com
        if on_domain(domain, "soundcloud.com"):
            if "/sets/" in path:
                return Platform.SOUNDCLOUD_SET
            return Platform.SOUNDCLOUD

        # HDRezka (rezka.ag и зеркала: hdrezka.me и т.п.)
        if on_domain(host, *_HDREZKA_DOMAINS):
            return Platform.HDREZKA

        # PornHub
        if on_domain(domain, "pornhub.com"):
            if "/shorties/" in path:
                return Platform.PORNHUB_SHORT
            return Platform.PORNHUB

        # TikTok (включая короткие ссылки vm./vt.)
        if on_domain(domain, "tiktok.com"):
            return Platform.TIKTOK

        # Pinterest (много доменов: .com/.ca/.co.uk + короткие pin.it)
        if _PINTEREST_RE.match(host) or on_domain(domain, "pin.it"):
            return Platform.PINTEREST

        # Instagram
        if on_domain(domain, "instagram.com", "ddinstagram.com"):
            if "/reel/" in path or "/reels/" in path:
                return Platform.INSTAGRAM_REEL
            if "/p/" in path or "/tv/" in path:
                return Platform.INSTAGRAM_POST

        # X (Twitter): поддерживаем ссылку на конкретный пост (/status/<id>).
        # Профили и прочие страницы не качаем. Зеркала fx/vx тоже принимаем.
        if on_domain(domain, "twitter.com", "x.com",
                  "fxtwitter.com", "vxtwitter.com", "fixupx.com"):
            if "/status/" in path:
                return Platform.TWITTER

        # Spotify: качаем через поиск трека на YouTube (напрямую DRM не даёт)
        if on_domain(domain, "spotify.com"):
            if "/album/" in path or "/playlist/" in path:
                return Platform.SPOTIFY_COLLECTION
            return Platform.SPOTIFY

    except Exception:
        pass

    return Platform.UNKNOWN


def _yt_id(parsed) -> str | None:
    """Идентификатор ролика YouTube из любой формы ссылки."""
    path = parsed.path
    if parsed.netloc.lower().replace("www.", "") == "youtu.be":
        return path.strip("/").split("/")[0] or None
    for prefix in ("/shorts/", "/embed/", "/live/", "/v/"):
        if path.startswith(prefix):
            return path[len(prefix):].split("/")[0] or None
    return dict(parse_qsl(parsed.query)).get("v")


def _first_segment_after(path: str, marker: str) -> str | None:
    """Сегмент пути сразу после marker: «/p/ABC/xyz» + «/p/» → «ABC»."""
    idx = path.find(marker)
    if idx == -1:
        return None
    return path[idx + len(marker):].split("/")[0] or None


def normalize_cache_url(url: str) -> str:
    """Приводит ссылку к каноничному виду ДЛЯ КЛЮЧА КЭША (сам URL для скачивания не меняем).

    Одну и ту же вещь присылают в десятке видов: с хвостами отслеживания (?si=, ?stkn=,
    ?utm_source=), с меткой времени, с мобильного домена, через зеркало, с под-путём.
    Для кэша всё это должно быть ОДНОЙ записью, иначе один и тот же ролик качается заново
    при каждом новом хвосте. У Spotify и Instagram хвост меняется при каждом «Поделиться»,
    так что без нормализации их прямые ссылки в кэш не попадали почти никогда.

    HDRezka сознательно не трогаем: там ключ составной (ссылка + озвучка + сезон + серия),
    и обрезка хвоста сломала бы его.
    """
    platform = detect_platform(url)
    try:
        parsed = urlparse(url)
        path = parsed.path

        if platform == Platform.TWITTER:
            m = _TW_STATUS_RE.search(path)
            # /<user>/status/<id> — путь до id включительно, домен → x.com
            return f"https://x.com{path[:m.end()]}" if m else url

        if platform in (Platform.YOUTUBE_VIDEO, Platform.YOUTUBE_SHORTS, Platform.YT_MUSIC):
            vid = _yt_id(parsed)
            if not vid:
                return url
            if platform == Platform.YT_MUSIC:
                return f"https://music.youtube.com/watch?v={vid}"
            kind = "shorts/" if platform == Platform.YOUTUBE_SHORTS else "watch?v="
            return f"https://www.youtube.com/{kind}{vid}"

        if platform in (Platform.INSTAGRAM_REEL, Platform.INSTAGRAM_POST):
            for marker, canon in (("/reels/", "reel"), ("/reel/", "reel"),
                                  ("/p/", "p"), ("/tv/", "p")):
                code = _first_segment_after(path, marker)
                if code:
                    return f"https://www.instagram.com/{canon}/{code}"
            return url

        if platform == Platform.TIKTOK:
            # Полную ссылку сводим к тому же ключу, что и путь через данные поста («tt:<id>»),
            # чтобы одно видео не лежало в кэше дважды. Короткие vt./vm. без сети не развернуть.
            vid = _first_segment_after(path, "/video/") or _first_segment_after(path, "/photo/")
            return f"tt:{vid}" if vid and vid.isdigit() else url

        if platform in (Platform.SPOTIFY, Platform.SPOTIFY_COLLECTION):
            for marker in ("/track/", "/album/", "/playlist/", "/episode/"):
                code = _first_segment_after(path, marker)
                if code:
                    return f"https://open.spotify.com{marker}{code}"
            return url

        if platform == Platform.PORNHUB:
            key = dict(parse_qsl(parsed.query)).get("viewkey")
            return f"https://www.pornhub.com/view_video.php?viewkey={key}" if key else url

        if platform == Platform.PORNHUB_SHORT:
            vid = _first_segment_after(path, "/shorties/")
            return f"https://www.pornhub.com/shorties/{vid}" if vid else url

        if platform in (Platform.SOUNDCLOUD, Platform.SOUNDCLOUD_SET, Platform.PINTEREST):
            # Здесь путь и есть идентификатор — достаточно отбросить хвосты.
            domain = parsed.netloc.lower().replace("www.", "").replace("m.", "")
            return f"https://{domain}{path.rstrip('/')}" if path.strip("/") else url

    except Exception:
        pass
    return url


def is_supported(url: str) -> bool:
    return detect_platform(url) != Platform.UNKNOWN
