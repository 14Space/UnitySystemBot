"""
Поиск источника трека для музыки со Spotify (Б1 + двухэтапный фолбэк).

Spotify отдаёт только метаданные (название, исполнитель, длительность) — сам звук
берём из внешнего источника по цепочке, пока не найдём чистый оригинал:
  1) YouTube — но «умно»: приоритет официальному каналу исполнителя / «- Topic»
     (официальное аудио), с отсевом slowed/sped/reverb и проверкой длительности;
  2) SoundCloud — ловит нишевое от самих авторов (то, чего нет на YouTube);
  3) None → вызывающий код падает на обычный YouTube-поиск (широкий запасной путь).

YouTube Music напрямую (ytmusicapi) не используем: анонимно он часто упирается в
consent-заглушку и отдаёт пусто. Официальное аудио и так доступно обычным поиском.
"""
import logging
import re

import yt_dlp

logger = logging.getLogger(__name__)


def _norm(s: str) -> str:
    """Нижний регистр, убираем пунктуацию — для сравнения названий."""
    return re.sub(r"[^\w\s]", " ", (s or "").lower())


def _title_matches(cand_title: str, orig_title: str) -> bool:
    """Все значимые слова названия трека должны быть в названии кандидата.
    Отсекает «не ту песню» (напр. другой трек того же исполнителя с похожей длиной)."""
    cand = set(_norm(cand_title).split())
    words = [w for w in _norm(orig_title).split() if len(w) > 1]
    if not words:
        return True
    return all(w in cand for w in words)

# Слова-маркеры «неоригинальных» версий. Отсеиваем, только если этого слова нет
# в самом названии трека из Spotify (вдруг он реально так называется).
_BAD = ["slowed", "sped up", "sped-up", "spedup", "speed up", "reverb",
        "nightcore", "8d audio", "bass boost", "bassboost", "super slow", "ultra slow"]


def _is_bad(text: str, orig_title: str) -> bool:
    c = (text or "").lower()
    o = (orig_title or "").lower()
    return any(w in c and w not in o for w in _BAD)


def _dur_ok(cand_dur, target) -> bool:
    """Длительность близка к оригиналу (±6%, но не меньше 8 сек допуска)."""
    if not target or not cand_dur:
        return True
    return abs(cand_dur - target) <= max(8, target * 0.06)


def _search_youtube(artist: str, title: str, duration: int) -> list[str]:
    """Умный поиск на YouTube: подходящие ролики, первым – официальное аудио исполнителя."""
    # Через общий механизм прокси: с дата-центрового IP YouTube отвечает на поиск
    # бот-чеком, и без этого Spotify-треки не находились вовсе (см. _needs_proxy).
    from bot.features.download.downloaders.ytdlp_wrapper import via_proxy

    term = f"ytsearch8:{artist} {title}"

    def _op(proxy_opts: dict):
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, **proxy_opts}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(term, download=False)

    try:
        res = via_proxy(term, _op)
    except Exception:
        logger.warning("YouTube поиск не удался", exc_info=True)
        return []

    art = (artist or "").lower()
    good = []
    for e in (res.get("entries") or []):
        if not e:
            continue
        up = e.get("uploader") or ""
        ttl = e.get("title", "")
        if _is_bad(f"{ttl} {up}", title):
            continue
        if not _title_matches(ttl, title):  # это точно тот трек, а не другой
            continue
        if not _dur_ok(e.get("duration"), duration):
            continue
        good.append(e)

    def score(e):
        up = (e.get("uploader") or "").lower()
        ttl = (e.get("title") or "").lower()
        official = bool(art) and (art in up or up.endswith("- topic"))
        is_audio = "official audio" in ttl or up.endswith("- topic")
        return (
            0 if official else 1,     # сначала официальный канал исполнителя
            0 if is_audio else 1,     # затем именно «аудио», а не клип
            abs((e.get("duration") or 0) - (duration or 0)),  # ближе по длительности
        )

    return [e.get("webpage_url") or f"https://www.youtube.com/watch?v={e.get('id')}"
            for e in sorted(good, key=score)]


def _search_soundcloud(artist: str, title: str, duration: int) -> list[str]:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "extract_flat": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            res = ydl.extract_info(f"scsearch8:{artist} {title}", download=False)
    except Exception:
        logger.warning("SoundCloud поиск не удался", exc_info=True)
        return []
    entries = [e for e in (res.get("entries") or []) if e]
    good = [e for e in entries
            if not _is_bad(e.get("title", ""), title)
            and _title_matches(e.get("title", ""), title)
            and _dur_ok(e.get("duration"), duration)]
    art = (artist or "").lower()
    # приоритет: трек от самого автора (uploader = исполнитель), затем ближе по длительности
    good.sort(key=lambda e: (
        0 if art and art in (e.get("uploader") or "").lower() else 1,
        abs((e.get("duration") or 0) - (duration or 0)),
    ))
    return [e.get("url") or e.get("webpage_url") for e in good]


def find_track_sources(artist: str, title: str, duration: int) -> list[str]:
    """Все подходящие ссылки на трек, лучшая первой (YouTube-официальное → SoundCloud).
    Нужен весь список: лучший вариант бывает с DRM (SoundCloud) или снят с YouTube,
    а перезалив того же трека рядом качается нормально."""
    return _search_youtube(artist, title, duration) or _search_soundcloud(artist, title, duration)


def find_track_source(artist: str, title: str, duration: int) -> str | None:
    """Лучшая ссылка для скачивания трека или None."""
    found = find_track_sources(artist, title, duration)
    return found[0] if found else None
