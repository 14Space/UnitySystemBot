import os
import time
import shutil
import glob
import subprocess
import requests
import yt_dlp


def _find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return os.path.dirname(found)
    pattern = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*\bin\ffmpeg.exe")
    matches = glob.glob(pattern)
    if matches:
        return os.path.dirname(matches[0])
    return None


FFMPEG_DIR = _find_ffmpeg()
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "data/downloads")

BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,  # глушим предупреждения yt-dlp (n challenge и т.п.) — лог чистый
    # android_vr не требует PO-токена, не попадает под SABR и отдаёт полный диапазон до 4K.
    # web_safari — запасной клиент.
    "extractor_args": {
        "youtube": {"player_client": ["android_vr", "web_safari"]},
    },
}
if FFMPEG_DIR:
    BASE_OPTS["ffmpeg_location"] = FFMPEG_DIR

# Прокси (из .env → PROXY_URL) применяем ТОЛЬКО к YT Music — она чаще всего под гео-блоком.
# Остальные загрузки идут напрямую, через обычную сеть.
_PROXY = os.getenv("PROXY_URL", "")


def _proxy_opts(url: str) -> dict:
    if _PROXY and "music.youtube.com" in (url or ""):
        return {"proxy": _PROXY}
    return {}


def get_video_info(url: str, allow_drm: bool = False) -> dict:
    """Получает информацию о видео без скачивания.
    allow_drm=True — не падать на DRM-треках, а вернуть метаданные (название, длительность)
    без самих форматов. Нужно, чтобы по названию найти трек на YouTube."""
    opts = dict(BASE_OPTS)
    if allow_drm:
        opts["ignore_no_formats_error"] = True
    opts.update(_proxy_opts(url))  # прокси только для YT Music
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


STANDARD_QUALITIES = [144, 240, 360, 480, 720, 1080, 1440, 2160]


def _snap_to_standard(height: int) -> int:
    """Привязывает любую высоту к ближайшему стандартному качеству (816 -> 720)"""
    return min(STANDARD_QUALITIES, key=lambda s: abs(s - height))


def get_available_qualities(info: dict) -> list[int]:
    """Возвращает список доступных качеств, приведённых к стандартным кнопкам"""
    buckets = set()
    for fmt in info.get("formats", []):
        h = fmt.get("height")
        vcodec = fmt.get("vcodec", "none")
        url = fmt.get("url", "")
        if h and vcodec != "none" and url:
            buckets.add(_snap_to_standard(h))
    return sorted(buckets)


def download_video(
    url: str,
    quality: int,
    progress_callback=None,
    postprocess_callback=None,
) -> str:
    """
    Скачивает видео в указанном качестве.
    info — уже полученные метаданные (чтобы не запрашивать YouTube второй раз).
    progress_callback(percent) — вызывается во время скачивания.
    postprocess_callback() — вызывается когда ffmpeg начинает склейку.
    """
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    output_path = os.path.join(DOWNLOADS_DIR, "%(id)s_%(height)sp_viaSaver.%(ext)s")

    last_reported = [-1]

    def progress_hook(d):
        if d["status"] == "downloading" and progress_callback:
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                percent = round(downloaded / total * 100)
                if percent >= last_reported[0] + 5:
                    last_reported[0] = percent
                    progress_callback(percent)

        elif d["status"] == "finished" and postprocess_callback:
            # Скачивание завершено, сейчас начнётся склейка ffmpeg
            postprocess_callback()

    # Запас 25%: ловим чуть завышенные высоты (816 при выборе 720),
    # но не перепрыгиваем на следующее стандартное качество
    cap = int(quality * 1.25)
    # iPhone/Telegram на iOS аппаратно декодирует только H.264 (avc1). YouTube же по
    # умолчанию отдаёт «лучшее» видео в VP9/AV1 — оно склеивается в mp4 без
    # перекодирования, и на айфоне получается чёрный экран при живом звуке. Поэтому
    # СНАЧАЛА просим H.264 (avc1) + AAC (mp4a), затем любой mp4, и лишь в крайнем
    # случае — что есть (перекодируем ниже, если кодек оказался несовместимым).
    fmt = (
        f"bestvideo[height<={cap}][vcodec^=avc1]+bestaudio[acodec^=mp4a]"
        f"/bestvideo[height<={cap}][ext=mp4]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={cap}]+bestaudio"
        f"/best[height<={cap}]"
    )

    ydl_opts = {
        **BASE_OPTS,
        "format": fmt,
        "outtmpl": output_path,
        "merge_output_format": "mp4",
        "progress_hooks": [progress_hook],
        # +faststart переносит метаданные в начало файла — видео играется на лету,
        # не дожидаясь полной загрузки на стороне зрителя
        "postprocessor_args": {"merger": ["-movflags", "+faststart"]},
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            extracted = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(extracted)
            if not os.path.exists(filename):
                filename = filename.rsplit(".", 1)[0] + ".mp4"
            # Страховка: если H.264 не нашлось (часто на 1440p/2160p — там только
            # VP9/AV1), перекодируем в H.264, иначе на iPhone будет чёрный экран.
            return _ensure_h264(filename, postprocess_callback)
    except Exception as e:
        # На Windows антивирус иногда держит .temp.mp4 в момент переименования
        # после склейки ffmpeg. Файл уже готов — переименовываем сами с повторами.
        if "WinError 32" in str(e) or isinstance(e, PermissionError):
            recovered = _rename_temp_file()
            if recovered:
                return recovered
        raise


# Кодеки, которые iPhone/Telegram на iOS играют аппаратно. Остальное (vp9, av01) —
# чёрный экран при живом звуке, поэтому перекодируем в h264.
_IOS_OK_CODECS = ("h264", "avc1", "hevc", "h265")


def _video_codec(path: str) -> str:
    """Имя видеокодека файла через ffprobe (пустая строка, если не удалось)."""
    ffprobe = os.path.join(FFMPEG_DIR, "ffprobe") if FFMPEG_DIR else "ffprobe"
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True,
        )
        return (out.stdout or "").strip().lower()
    except Exception:
        return ""


def _ensure_h264(path: str, postprocess_callback=None) -> str:
    """
    Гарантирует совместимый с iOS видеокодек. Если файл уже H.264/HEVC — отдаём как есть.
    Иначе (VP9/AV1) перекодируем в H.264 + yuv420p с faststart. При ошибке возвращаем оригинал.
    """
    codec = _video_codec(path)
    if not codec or codec.startswith(_IOS_OK_CODECS):
        return path

    if postprocess_callback:
        postprocess_callback()  # покажем пользователю «обработка» — перекодирование не мгновенно

    ffmpeg = os.path.join(FFMPEG_DIR, "ffmpeg") if FFMPEG_DIR else "ffmpeg"
    out_path = path.rsplit(".", 1)[0] + "_h264.mp4"
    cmd = [
        ffmpeg, "-y", "-i", path,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        out_path,
    ]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        try:
            os.remove(path)
        except OSError:
            pass
        return out_path
    return path  # перекодировать не вышло — отдаём оригинал, чтобы хоть что-то ушло


def _rename_temp_file() -> str | None:
    """Находит свежий <имя>.temp.mp4 и переименовывает в <имя>.mp4 с повторами."""
    temps = glob.glob(os.path.join(DOWNLOADS_DIR, "*.temp.mp4"))
    if not temps:
        return None
    temp_path = max(temps, key=os.path.getmtime)
    final_path = temp_path.replace(".temp.mp4", ".mp4")
    for _ in range(20):  # до ~10 секунд ожидания пока антивирус отпустит файл
        try:
            if os.path.exists(final_path):
                os.remove(final_path)
            os.replace(temp_path, final_path)
            return final_path
        except PermissionError:
            time.sleep(0.5)
    return None


def search_audio(query: str, target_duration: int = None, count: int = 5) -> str:
    """
    Ищет трек на YouTube и возвращает ссылку на ЛУЧШЕЕ совпадение.
    Если известна длительность (из Spotify) — выбираем результат с самой близкой длиной,
    это спасает от случайных «не тех» треков (каверы, ремиксы, ускоренные версии).
    """
    opts = {**BASE_OPTS, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(f"ytsearch{count}:{query}", download=False)

    entries = [e for e in (res.get("entries") or []) if e]
    if not entries:
        raise ValueError(f"YouTube ничего не нашёл по запросу: {query}")

    if target_duration:
        entries.sort(key=lambda e: abs((e.get("duration") or 0) - target_duration))

    best = entries[0]
    return best.get("webpage_url") or f"https://www.youtube.com/watch?v={best['id']}"


def search_audio_candidates(query: str, target_duration: int = None, count: int = 5) -> list[str]:
    """Несколько лучших совпадений (отсортированы по близости длительности).
    Нужно, чтобы при недоступности первого результата попробовать следующий."""
    opts = {**BASE_OPTS, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
    entries = [e for e in (res.get("entries") or []) if e]
    if target_duration:
        entries.sort(key=lambda e: abs((e.get("duration") or 0) - target_duration))
    return [e.get("webpage_url") or f"https://www.youtube.com/watch?v={e['id']}" for e in entries]


def get_soundcloud_set(url: str) -> dict:
    """Читает альбом/плейлист (set) SoundCloud: название и список треков с реальными именами.
    Полное чтение (не flat) — иначе у части треков вместо названия числовой id.
    ignore_no_formats_error — чтобы DRM-треки тоже отдавали название."""
    opts = {**BASE_OPTS, "ignore_no_formats_error": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    tracks = []
    cover = info.get("thumbnail")
    for e in (info.get("entries") or []):
        if not e:
            continue
        track_url = e.get("webpage_url") or e.get("url")
        if track_url:
            tracks.append({
                "title": e.get("title") or _title_from_url(track_url),
                "url": track_url,
                "duration": int(e.get("duration") or 0),
            })
            # если у сета своей обложки нет — берём обложку первого трека
            if not cover:
                cover = e.get("thumbnail")
    return {"title": info.get("title") or "Сет", "tracks": tracks, "cover": cover}


def _title_from_url(url: str) -> str:
    """Делает читаемое название из последней части ссылки: .../new-religion -> New Religion"""
    slug = url.rstrip("/").split("/")[-1].split("?")[0]
    return slug.replace("-", " ").replace("_", " ").strip().title() or "Без названия"


def download_audio(
    url: str, progress_callback=None, postprocess_callback=None, embed_thumbnail: bool = True,
) -> str:
    """
    Скачивает аудио (SoundCloud, YT Music) и конвертирует в mp3 с тегами и обложкой.
    progress_callback(percent) — во время скачивания.
    postprocess_callback() — когда ffmpeg начинает конвертацию в mp3.
    embed_thumbnail=False — НЕ вшивать обложку с источника (мы поставим свою отдельно).
        Иначе в mp3 окажутся две обложки и Telegram покажет в кружке не ту.
    """
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    output_path = os.path.join(DOWNLOADS_DIR, "%(title)s_viaSaver.%(ext)s")

    last_reported = [-1]

    def progress_hook(d):
        if d["status"] == "downloading" and progress_callback:
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                percent = round(downloaded / total * 100)
                if percent >= last_reported[0] + 5:
                    last_reported[0] = percent
                    progress_callback(percent)
        elif d["status"] == "finished" and postprocess_callback:
            postprocess_callback()

    postprocessors = [
        # извлекаем звук и кодируем в mp3 максимального качества
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
        # вшиваем теги (название, исполнитель)
        {"key": "FFmpegMetadata", "add_metadata": True},
    ]
    if embed_thumbnail:
        # приводим обложку к jpg (webp в mp3 не вшивается) и вшиваем её
        postprocessors.insert(1, {"key": "FFmpegThumbnailsConvertor", "format": "jpg"})
        postprocessors.append({"key": "EmbedThumbnail"})

    ydl_opts = {
        **BASE_OPTS,
        "format": "bestaudio/best",
        "outtmpl": output_path,
        "progress_hooks": [progress_hook],
        "writethumbnail": embed_thumbnail,  # обложку источника качаем только если вшиваем
        "postprocessors": postprocessors,
        **_proxy_opts(url),  # прокси только для YT Music
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        extracted = ydl.extract_info(url, download=True)
        # ytsearch (Spotify) возвращает "плейлист" — берём первый реальный трек
        if "entries" in extracted:
            extracted = extracted["entries"][0]
        filename = ydl.prepare_filename(extracted)
        # после конвертации исходное расширение (webm/m4a) заменяется на mp3
        mp3_path = filename.rsplit(".", 1)[0] + ".mp3"
        if os.path.exists(mp3_path):
            return mp3_path
        return filename


def download_media(url: str) -> str:
    """
    Универсальное скачивание одного медиа (TikTok, Pinterest): видео или фото.
    Возвращает путь к файлу (ext подскажет тип: mp4 — видео, jpg — фото).
    """
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    output_path = os.path.join(DOWNLOADS_DIR, "%(id)s_viaSaver.%(ext)s")

    ydl_opts = {
        **BASE_OPTS,
        "outtmpl": output_path,
        "merge_output_format": "mp4",
        # фото-пины Pinterest без видео — не падаем
        "ignore_no_formats_error": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # Сначала смотрим метаданные: есть ли вообще видео
        info = ydl.extract_info(url, download=False)
        formats = info.get("formats") or []
        has_video = any(f.get("vcodec") not in (None, "none") for f in formats)

        if has_video:
            info = ydl.extract_info(url, download=True)
            for d in (info.get("requested_downloads") or []):
                fp = d.get("filepath")
                if fp and os.path.exists(fp):
                    return fp
            filename = ydl.prepare_filename(info)
            if not os.path.exists(filename):
                filename = filename.rsplit(".", 1)[0] + ".mp4"
            return filename

        # Видео нет (фото-пин) — качаем картинку напрямую, СОХРАНЯЯ реальное расширение
        # (важно для .gif — иначе анимация теряется и шлётся как статичное фото)
        image_url = _best_image_url(info)
        if image_url:
            ext = os.path.splitext(image_url.split("?")[0])[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                ext = ".jpg"
            path = os.path.join(DOWNLOADS_DIR, f"{info.get('id', 'media')}_viaSaver{ext}")
            content = requests.get(image_url, timeout=60).content
            with open(path, "wb") as f:
                f.write(content)
            return path

        raise ValueError("В этом пине нет медиа")


def convert_gif_to_mp4(gif_path: str) -> str:
    """Конвертирует GIF в чистый mp4 (H.264) — чтобы Telegram не пере-сжимал грубо.
    Если не вышло — возвращает исходный gif."""
    mp4 = gif_path.rsplit(".", 1)[0] + "_anim.mp4"
    ffmpeg = os.path.join(FFMPEG_DIR, "ffmpeg") if FFMPEG_DIR else "ffmpeg"
    cmd = [
        ffmpeg, "-y", "-i", gif_path,
        "-movflags", "faststart", "-pix_fmt", "yuv420p",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-crf", "18",
        mp4,
    ]
    subprocess.run(cmd, capture_output=True)
    if os.path.exists(mp4) and os.path.getsize(mp4) > 0:
        return mp4
    return gif_path


def _best_image_url(info: dict) -> str | None:
    """Лучшая (самая большая) картинка из метаданных — для фото-пинов Pinterest."""
    # thumbnail у Pinterest указывает на оригинал (/originals/...)
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = info.get("thumbnails") or []
    if thumbs:
        best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        return best.get("url")
    return None


def download_shorts(url: str) -> str:
    """Скачивает Shorts в максимальном качестве"""
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    output_path = os.path.join(DOWNLOADS_DIR, "%(id)s_viaSaver.%(ext)s")

    fmt = "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"

    ydl_opts = {
        **BASE_OPTS,
        "format": fmt,
        "outtmpl": output_path,
        "merge_output_format": "mp4",
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        extracted = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(extracted)
        if not os.path.exists(filename):
            filename = filename.rsplit(".", 1)[0] + ".mp4"
        return filename
