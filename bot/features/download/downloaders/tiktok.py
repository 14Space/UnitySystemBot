import os
import re
import subprocess
import time
import requests
from bot.features.download.downloaders.ytdlp_wrapper import DOWNLOADS_DIR, FFMPEG_DIR

# Публичный API без авторизации: отдаёт видео без водяного знака и слайдшоу.
# yt-dlp web-парсинг TikTok нестабилен (анти-бот), поэтому идём через него.
API = "https://www.tikwm.com/api/"
# Запасной сервис на случай, если основной не отдал видео (свои серверы — может
# вытащить то, что не смог tikwm). Отдаёт только видео (не слайдшоу).
BACKUP_API = "https://lovetik.com/api/ajax/search"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def _abs(url: str) -> str:
    """tikwm иногда отдаёт относительный путь (/video/...) — дополняем доменом."""
    if url.startswith("/"):
        return "https://www.tikwm.com" + url
    return url


def _ffbin(name: str) -> str:
    return os.path.join(FFMPEG_DIR, name) if FFMPEG_DIR else name


def _safe_remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def _audio_duration(path: str) -> float:
    """Длительность аудио в секундах через ffprobe."""
    try:
        out = subprocess.run(
            [_ffbin("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True,
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def _build_slideshow(images: list[str], audio: str, out_path: str) -> str:
    """
    Собирает видео-слайдшоу: каждая картинка показывается равную долю длины музыки,
    музыка идёт фоном. Картинки приводятся к единому холсту 1080x1920.
    """
    n = len(images)
    total = _audio_duration(audio) or n * 3.0
    per = max(total / n, 1.0)

    # Каждая картинка — отдельный вход (показывается per секунд), масштабируется
    # независимо к холсту 1080x1920, потом всё склеивается concat-фильтром.
    cmd = [_ffbin("ffmpeg"), "-y"]
    for img in images:
        cmd += ["-loop", "1", "-t", f"{per:.3f}", "-i", img]
    cmd += ["-i", audio]  # аудио — последний вход (индекс n)

    parts = []
    for i in range(n):
        parts.append(
            f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{i}]"
        )
    concat = "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
    filtergraph = ";".join(parts) + ";" + concat

    cmd += [
        "-filter_complex", filtergraph,
        "-map", "[v]", "-map", f"{n}:a",
        "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
        "-shortest", out_path,
    ]
    subprocess.run(cmd, capture_output=True)
    return out_path


def _resolve_short(url: str) -> str:
    """Разворачивает короткую ссылку (vt./vm.tiktok.com) в полную — так API надёжнее.
    Если не вышло — возвращаем исходную ссылку как есть."""
    try:
        if url and ("vt.tiktok.com" in url or "vm.tiktok.com" in url):
            return requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True).url
    except Exception:
        pass
    return url


def _api_call(url: str) -> dict:
    return requests.get(API, params={"url": url, "hd": 1}, headers=HEADERS, timeout=30).json()


def _fetch_backup(url: str) -> dict | None:
    """Запасной сервис (lovetik): когда основной не отдал видео. Возвращает данные
    в том же формате, что и основной (kind='video'), или None. Слайдшоу не умеет."""
    try:
        j = requests.post(BACKUP_API, data={"query": url}, headers=HEADERS, timeout=25).json()
    except Exception:
        return None
    if j.get("status") != "ok":
        return None
    # ft==1 — варианты БЕЗ водяного знака; берём лучшее качество (последнее в списке)
    nowm = [ln for ln in (j.get("links") or []) if ln.get("ft") == 1 and ln.get("a")]
    if not nowm:
        return None
    m = re.search(r"/video/(\d+)", url)
    item_id = m.group(1) if m else "tiktok"
    play = nowm[-1]["a"]
    return {"id": item_id, "kind": "video", "data": {"id": item_id, "play": play, "hdplay": play}}


def fetch_tiktok(url: str) -> dict:
    """Запрашивает данные поста (без скачивания файлов) и определяет тип:
    'video' — обычное видео, 'slideshow' — набор фото (+ возможно музыка),
    'live' — Live Photo (короткие видео). Возвращает {'id','kind','data'}."""
    url = _resolve_short(url)
    payload = _api_call(url)
    # Отказ бывает из-за мелочей: временный сбой сервиса или лимит «1 запрос/сек».
    # Ждём секунду и пробуем ещё раз — пользователь заминки не замечает.
    if payload.get("code") != 0:
        time.sleep(1.2)
        payload = _api_call(url)
    if payload.get("code") == 0:
        data = payload["data"]
        if data.get("live_images"):
            kind = "live"
        elif data.get("images"):
            kind = "slideshow"
        else:
            kind = "video"
        return {"id": str(data.get("id", "tiktok")), "kind": kind, "data": data}

    # Основной сервис не смог — пробуем запасной (только видео)
    backup = _fetch_backup(url)
    if backup:
        return backup
    raise ValueError(payload.get("msg") or "TikTok API error")


def _download_images(images: list[str], item_id: str) -> list[str]:
    files = []
    for i, img_url in enumerate(images, 1):
        content = requests.get(_abs(img_url), headers=HEADERS, timeout=60).content
        path = os.path.join(DOWNLOADS_DIR, f"{item_id}_{i}_viaSaver.jpg")
        with open(path, "wb") as f:
            f.write(content)
        files.append(path)
    return files


def download_from(info: dict, mode: str = "auto") -> list[str]:
    """
    Скачивает TikTok по уже полученным данным (fetch_tiktok).
    mode для слайдшоу: 'photos' — только фото, 'video' — собрать видео со звуком,
    'auto' — видео, если есть музыка, иначе фото. Возвращает список файлов.
    """
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    data = info["data"]
    item_id = info["id"]

    # Live Photo: набор коротких видео — отдаём альбомом видео
    if info["kind"] == "live":
        files = []
        for i, vid_url in enumerate(data["live_images"], 1):
            content = requests.get(_abs(vid_url), headers=HEADERS, timeout=120).content
            path = os.path.join(DOWNLOADS_DIR, f"{item_id}_{i}_viaSaver.mp4")
            with open(path, "wb") as f:
                f.write(content)
            files.append(path)
        return files

    # Слайдшоу — набор фото (+ возможно музыка)
    if info["kind"] == "slideshow":
        files = _download_images(data["images"], item_id)
        # Только фото — отдаём картинки как есть
        if mode == "photos":
            return files
        # Видео или авто: собираем слайдшоу-видео, если есть музыка
        music_url = data.get("music")
        if music_url:
            try:
                audio_path = os.path.join(DOWNLOADS_DIR, f"{item_id}_audio.mp3")
                content = requests.get(_abs(music_url), headers=HEADERS, timeout=60).content
                with open(audio_path, "wb") as f:
                    f.write(content)
                video_path = os.path.join(DOWNLOADS_DIR, f"{item_id}_viaSaver.mp4")
                _build_slideshow(files, audio_path, video_path)
                if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
                    for f in files:
                        _safe_remove(f)
                    _safe_remove(audio_path)
                    return [video_path]
                _safe_remove(audio_path)
            except Exception:
                pass  # не вышло собрать видео — отдадим картинки
        return files  # музыки нет (или сборка не удалась) — отдаём фото

    # Обычное видео
    play = data.get("hdplay") or data.get("play")
    content = requests.get(_abs(play), headers=HEADERS, timeout=120).content
    path = os.path.join(DOWNLOADS_DIR, f"{item_id}_viaSaver.mp4")
    with open(path, "wb") as f:
        f.write(content)
    return [path]


def download_tiktok(url: str, mode: str = "auto") -> list[str]:
    """Скачивает TikTok одним вызовом (данные + файлы). Обёртка над fetch_tiktok+download_from."""
    return download_from(fetch_tiktok(url), mode)
