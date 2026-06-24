import os
import re
import subprocess
import requests
from HdRezkaApi import HdRezkaApi
from worker.downloaders.ytdlp_wrapper import DOWNLOADS_DIR, FFMPEG_DIR

# Коды языков субтитров → метка для контейнера mp4
_LANG = {"ru": "rus", "en": "eng", "ua": "ukr", "uk": "ukr"}


def parse_season_episode(url: str) -> tuple[int | None, int | None]:
    """Достаёт сезон/серию из хвоста ссылки сериала: #t:355-s:1-e:1"""
    s = re.search(r"s:(\d+)", url)
    e = re.search(r"e:(\d+)", url)
    return (int(s.group(1)) if s else None, int(e.group(1)) if e else None)


def open_media(url: str) -> HdRezkaApi:
    """Создаёт объект HDRezka (одна загрузка страницы). Переиспользуем на всех шагах."""
    return HdRezkaApi(url)


def get_info(r: HdRezkaApi, url: str) -> dict:
    """Название, тип (фильм/сериал), список озвучек, сезон/серия (для сериала)."""
    is_series = "series" in str(r.type)
    season, episode = parse_season_episode(url)
    if is_series:
        season = season or 1
        episode = episode or 1
    # Убираем «(+субтитры)» из названия — в Telegram мягкие субтитры не видны,
    # чтобы не сбивать с толку. Сами субтитры всё равно вшиваем в файл.
    def _clean(n: str) -> str:
        return n.replace(" (+субтитры)", "").replace(" (+subtitles)", "").strip()

    # Премиум-озвучки (только по подписке HDRezka) не показываем.
    # У сериала озвучки зависят от конкретной серии — берём её набор, а не общий.
    if is_series:
        eps = next((s["episodes"] for s in r.episodesInfo if s["season"] == season), [])
        ep = next((e for e in eps if e["episode"] == episode), None)
        raw = ep["translations"] if ep else []
        translators = [
            (t["translator_id"], _clean(t["translator_name"]))
            for t in raw if not t.get("premium")
        ]
    else:
        translators = [
            (tid, _clean(info["name"]))
            for tid, info in r.translators.items()
            if not info.get("premium")
        ]
    return {
        "name": r.name,
        "is_series": is_series,
        "translators": translators,
        "season": season,
        "episode": episode,
        "thumbnail": getattr(r, "thumbnailHQ", None) or getattr(r, "thumbnail", None),
    }


def get_stream(r: HdRezkaApi, translation: int, season=None, episode=None):
    """Поток для озвучки (одна сетевая операция). Объект потока кэшируем и потом качаем из него."""
    return r.getStream(season=season, episode=episode, translation=translation)


def stream_qualities(stream) -> list[str]:
    """Список качеств у уже полученного потока."""
    return list(stream.videos.keys())


def _download_file(url: str, path: str):
    with requests.get(url, stream=True, timeout=180) as resp:
        resp.raise_for_status()
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)


def _safe_rm(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def _mux_subtitles(video: str, subs: list, out_path: str) -> bool:
    """Вшивает субтитры мягкой дорожкой (mov_text) — без перекодирования видео."""
    ffmpeg = os.path.join(FFMPEG_DIR, "ffmpeg") if FFMPEG_DIR else "ffmpeg"
    cmd = [ffmpeg, "-y", "-i", video]
    for _, sub_path in subs:
        cmd += ["-i", sub_path]
    cmd += ["-map", "0"]
    for i in range(1, len(subs) + 1):
        cmd += ["-map", str(i)]
    cmd += ["-c", "copy", "-c:s", "mov_text"]
    for idx, (code, _) in enumerate(subs):
        cmd += [f"-metadata:s:s:{idx}", f"language={_LANG.get(code, code)}"]
    cmd += [out_path]
    res = subprocess.run(cmd, capture_output=True)
    return res.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0


def download_stream(stream, quality: str, name="video", season=None, episode=None) -> str:
    """Качает видео из уже полученного потока (без повторного запроса).
    Если у потока есть субтитры — тихо вшиваем их мягкими дорожками."""
    value = stream.videos[quality]
    video_url = value[0] if isinstance(value, (list, tuple)) else value

    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    safe = re.sub(r"[^\w]+", "_", name)[:40] or "video"
    suffix = f"_s{season}e{episode}" if season and episode else ""
    base = os.path.join(DOWNLOADS_DIR, f"{safe}{suffix}_viaSaver")
    raw = base + "_raw.mp4"
    final = base + ".mp4"

    _download_file(video_url, raw)

    # Вшиваем все доступные субтитры (мягкими дорожками)
    subs = []
    try:
        sub_dict = stream.subtitles.subtitles or {}
    except Exception:
        sub_dict = {}
    for code, info in sub_dict.items():
        link = info.get("link")
        if link:
            sub_path = base + f".{code}.vtt"
            try:
                _download_file(link, sub_path)
                subs.append((code, sub_path))
            except Exception:
                pass

    if subs and _mux_subtitles(raw, subs, final):
        _safe_rm(raw)
        for _, sp in subs:
            _safe_rm(sp)
        return final

    # Субтитров нет или вшить не удалось — отдаём видео как есть
    for _, sp in subs:
        _safe_rm(sp)
    os.replace(raw, final)
    return final
