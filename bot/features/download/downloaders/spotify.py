import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from bot.config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET

# Один клиент на весь процесс. Режим Client Credentials — без логина пользователя,
# только чтение публичных данных (название трека, исполнитель, длительность).
_client: spotipy.Spotify | None = None


def _get_client() -> spotipy.Spotify:
    global _client
    if _client is None:
        auth = SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
        )
        _client = spotipy.Spotify(auth_manager=auth)
    return _client


def get_track_info(url: str) -> dict:
    """
    Читает метаданные трека Spotify по ссылке.
    Возвращает title, artist, duration и поисковый запрос для YouTube.
    """
    sp = _get_client()
    track = sp.track(url)

    title = track["name"]
    artists = [a["name"] for a in track["artists"]]
    artist = ", ".join(artists)
    duration = round(track["duration_ms"] / 1000)
    images = track["album"].get("images") or []
    cover = images[0]["url"] if images else None

    # Запрос для поиска на YouTube: "Исполнитель Название"
    search_query = f"{artists[0]} {title}"

    return {
        "title": title,
        "artist": artist,
        "duration": duration,
        "search_query": search_query,
        "cover": cover,
    }


def _track_to_dict(track: dict, fallback_cover: str = None) -> dict | None:
    """Превращает трек Spotify в наш формат. None — если трека нет (удалён/локальный).
    fallback_cover — обложка альбома (у треков альбома нет своей картинки)."""
    if not track or not track.get("id"):
        return None
    artists = [a["name"] for a in track["artists"]]
    images = (track.get("album") or {}).get("images") or []
    cover = images[0]["url"] if images else fallback_cover
    return {
        "id": track["id"],
        "title": track["name"],
        "artist": ", ".join(artists),
        "duration": round(track["duration_ms"] / 1000),
        "query": f"{artists[0]} {track['name']}",
        "cover": cover,
    }


def get_collection_info(url: str) -> dict:
    """
    Читает альбом или плейлист Spotify целиком (с учётом постраничной отдачи API).
    Возвращает kind, title и список треков.
    """
    sp = _get_client()

    if "/album/" in url:
        album = sp.album(url)
        title = album["name"]
        kind = "Альбом"
        images = album.get("images") or []
        cover = images[0]["url"] if images else None
        page = album["tracks"]
        raw = list(page["items"])
        while page.get("next"):
            page = sp.next(page)
            raw.extend(page["items"])
        # у треков альбома нет своей картинки — подставляем обложку альбома
        tracks = [_track_to_dict(t, fallback_cover=cover) for t in raw]
    else:
        # Имя и обложку плейлиста берём отдельным лёгким запросом —
        # так надёжнее: основной объект иногда приходит без поля "tracks"
        meta = sp.playlist(url, fields="name,images")
        title = meta.get("name", "Плейлист")
        kind = "Плейлист"
        images = meta.get("images") or []
        cover = images[0]["url"] if images else None
        # Треки тянем отдельным эндпоинтом с пагинацией
        page = sp.playlist_items(url, additional_types=("track",))
        items = list(page["items"])
        while page.get("next"):
            page = sp.next(page)
            items.extend(page["items"])
        # в плейлисте трек лежит внутри поля "track", у него своя обложка альбома
        tracks = [_track_to_dict(it.get("track")) for it in items]

    tracks = [t for t in tracks if t]  # выкидываем пустые (удалённые/локальные)
    total_duration = sum(t["duration"] for t in tracks)

    return {
        "kind": kind,
        "title": title,
        "tracks": tracks,
        "total_duration": total_duration,
        "cover": cover,
    }
