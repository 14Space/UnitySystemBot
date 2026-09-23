import os
import re
import subprocess
import threading
import time
import uuid
import requests
from bot.features.download.downloaders.ytdlp_wrapper import DOWNLOADS_DIR, FFMPEG_DIR
from bot.utils import media_names, net
from bot.utils.ffmpeg_limits import FFMPEG_TIMEOUT, FFPROBE_TIMEOUT

try:
    from bot.config import SLIDE_SEC, SLIDE_AUDIO_FADE_SEC
except Exception:  # worker может запускаться отдельно от бота
    SLIDE_SEC = float(os.getenv("SLIDE_SEC", "3.0"))
    SLIDE_AUDIO_FADE_SEC = float(os.getenv("SLIDE_AUDIO_FADE_SEC", "1.5"))

# Публичный API без авторизации: отдаёт видео без водяного знака и слайдшоу.
# yt-dlp web-парсинг TikTok нестабилен (анти-бот), поэтому идём через него.
API = "https://www.tikwm.com/api/"
# Запасной сервис на случай, если основной не отдал видео (свои серверы — может
# вытащить то, что не смог tikwm). Отдаёт только видео (не слайдшоу).
BACKUP_API = "https://lovetik.com/api/ajax/search"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Прокси (PROXY_URL из .env) — тот же, что у YouTube и PornHub. Нужен потому, что
# tikwm режет дата-центровые адреса: с сервера во Франции API отдаёт ПУСТОЙ ответ
# (падает .json()), а через домашний адрес тот же запрос отвечает нормально. Это не
# отказ сервиса, а блок по IP, поэтому лечится именно прокси.
# Логика как у YouTube: сперва напрямую (дома и на чистом IP прокси не нужен и только
# замедлил бы), при отказе — повтор через прокси.
_PROXY = os.getenv("PROXY_URL", "")
_PROXY_FIRST = os.getenv("PROXY_FIRST", "false").lower() in ("1", "true", "yes")
_PROXIES = {"http": _PROXY, "https": _PROXY} if _PROXY else None


def _via(fn, *args, **kwargs):
    """Сетевой запрос с откатом на прокси: сначала напрямую, при ошибке — через прокси.
    При PROXY_FIRST прямую попытку пропускаем. Прокси не задан — обычный вызов."""
    if _PROXY_FIRST and _PROXIES:
        return fn(*args, proxies=_PROXIES, **kwargs)
    try:
        return fn(*args, **kwargs)
    except Exception:
        if not _PROXIES:
            raise
        return fn(*args, proxies=_PROXIES, **kwargs)


def _fetch_file(url: str, path: str, timeout: int = 120) -> str:
    """Качает файл НА ДИСК (потоком) с тем же откатом на прокси, что и остальные
    запросы модуля. Раньше каждый файл сначала целиком оказывался в памяти
    (`requests.get(...).content`), а на видео это уже десятки мегабайт на каждую из
    шести одновременных загрузок — см. bot/utils/net.py.
    """
    return _via(net.fetch_to_file, _abs(url), path, headers=HEADERS, timeout=timeout)


def _via_json(fn, *args, **kwargs):
    """То же, но для ответов JSON — и разбор тоже внутри попытки.

    Иначе откат не сработал бы вовсе: заблокированный tikwm отвечает не ошибкой, а
    ПУСТЫМ телом с кодом 200. Сам запрос при этом успешен, падает только .json(). Если
    разбирать снаружи, прямая попытка будет считаться удачной, и до прокси дело никогда
    не дойдёт — ровно тот случай, ради которого прокси здесь и появился.

    При PROXY_FIRST прямую попытку пропускаем совсем.
    """
    if _PROXY_FIRST and _PROXIES:
        return fn(*args, proxies=_PROXIES, **kwargs).json()
    try:
        return fn(*args, **kwargs).json()
    except Exception:
        if not _PROXIES:
            raise
        return fn(*args, proxies=_PROXIES, **kwargs).json()

# Короткий кэш ответов API: {url: (время, результат)}. Нужен, чтобы один и тот же
# пост в рамках одного запроса (контент + аудиодорожка) не запрашивался дважды.
# TTL маленький — CDN-ссылки внутри живут недолго, а нам они нужны сразу.
_FETCH_CACHE: dict[str, tuple[float, dict]] = {}
# Замок к кэшу: за ним ходят разные потоки (скачивания идут через asyncio.to_thread).
_CACHE_LOCK = threading.Lock()
_FETCH_TTL = 120  # секунд


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


def download_music(url: str) -> tuple[str, str] | None:
    """Скачивает оригинальный звук поста TikTok (видео или слайдшоу) как mp3.
    Возвращает (путь, «Автор – Название») или None. Используется для отдельной
    аудиодорожки, когда включён соответствующий тумблер в /setconfig."""
    info = fetch_tiktok(url)
    data = info["data"]
    music_url = data.get("music")
    if not music_url:
        return None
    # Уникальный хвост: один и тот же звук могут попросить сразу несколько человек,
    # и с общим именем один переписывал бы исходник, пока другой его перекодирует.
    tag = uuid.uuid4().hex[:8]
    raw = os.path.join(DOWNLOADS_DIR, f"{info['id']}_{tag}_track_src")
    _fetch_file(music_url, raw, timeout=60)
    # Приводим к чистому mp3 (звук из tikwm бывает в контейнере m4a/без тегов).
    out = os.path.join(DOWNLOADS_DIR, f"{info['id']}_{tag}_track.mp3")
    subprocess.run(
        [_ffbin("ffmpeg"), "-y", "-i", raw, "-vn", "-acodec", "libmp3lame",
         "-b:a", "192k", out],
        capture_output=True, timeout=FFMPEG_TIMEOUT,
    )
    _safe_remove(raw)
    if not (os.path.exists(out) and os.path.getsize(out) > 0):
        return None
    mi = data.get("music_info") or {}
    author = (mi.get("author") or "").strip()
    title = (mi.get("title") or "").strip()
    # У авторского звука название — служебная заглушка вида «original sound - nimpliq»,
    # то есть тот же автор второй раз. Клеить их значило бы получить «Nimpliq – original
    # sound - nimpliq». Различить помогает флаг original: у лицензированного трека там
    # False, и тогда название настоящее («Shape of You») — его и берём.
    boiler = mi.get("original") or title.lower().startswith("original sound")
    if boiler:
        nice = f"{author} – original sound" if author else "original sound"
    else:
        nice = f"{author} – {title}" if author and title else (title or author or "original sound")
    # У звука название есть всегда (у tikwm это music_info.title), поэтому имя файла
    # строим по нему, а не по номеру — в отличие от самого ролика.
    media_names.remember(out, nice, str(info.get("id") or ""))
    return out, nice


def _media_duration(path: str) -> float:
    """Длительность медиафайла (аудио или видео) в секундах через ffprobe."""
    try:
        out = subprocess.run(
            [_ffbin("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT,
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def _audio_tail_filter(video_len: float) -> str:
    """Фильтр для музыки: обрезаем под длину видео и плавно гасим хвост,
    чтобы трек не обрывался резко. Длительность затухания — из настроек."""
    fade = min(SLIDE_AUDIO_FADE_SEC, video_len / 2)
    trim = f"atrim=0:{video_len:.3f},asetpts=PTS-STARTPTS"
    if fade <= 0:                                   # затухание отключено настройкой
        return f"{trim}[a]"
    start = max(video_len - fade, 0)
    return f"{trim},afade=t=out:st={start:.3f}:d={fade:.3f}[a]"


def _build_slideshow(images: list[str], audio: str, out_path: str) -> str:
    """
    Собирает видео-слайдшоу: каждая картинка показывается SLIDE_SEC секунд,
    музыка идёт фоном и гасится в конце. Картинки приводятся к холсту 1080x1920.

    Одна картинка — особый случай: резать нечего, поэтому трек играет целиком,
    а кадр висит всю его длину (обрезка и затухание не применяются).
    """
    n = len(images)
    per = SLIDE_SEC
    single_full_audio = False

    if n == 1:
        audio_len = _media_duration(audio)
        if audio_len > 0:
            per = audio_len            # кадр висит столько, сколько звучит трек
            single_full_audio = True

    # Хвост под затухание: каждый слайд идёт per секунд на обычной громкости, а в
    # самом конце добавляем ещё tail секунд (держим последний кадр), чтобы музыка
    # успела плавно погаснуть, НЕ «съедая» 3с последнего слайда. При выключенном
    # затухании или одном фото (звук целиком) хвост не нужен.
    tail = 0.0 if single_full_audio else max(SLIDE_AUDIO_FADE_SEC, 0.0)
    video_len = per * n + tail

    # Каждая картинка — отдельный вход (показывается per секунд), масштабируется
    # независимо к холсту 1080x1920, потом всё склеивается concat-фильтром.
    cmd = [_ffbin("ffmpeg"), "-y"]
    for img in images:
        cmd += ["-loop", "1", "-t", f"{per:.3f}", "-i", img]
    if single_full_audio:
        cmd += ["-i", audio]                    # звук целиком, зацикливать нечего
    else:
        # Музыку зацикливаем: если трек короче слайдшоу, звук не оборвётся на полпути
        cmd += ["-stream_loop", "-1", "-i", audio]  # аудио — последний вход (индекс n)

    parts = []
    for i in range(n):
        parts.append(
            f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{i}]"
        )
    # При хвосте склеиваем в промежуточный [vcat] и держим последний кадр tail секунд
    vlabel = "[vcat]" if tail > 0 else "[v]"
    concat = "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0{vlabel}"
    filtergraph = ";".join(parts) + ";" + concat
    if tail > 0:
        filtergraph += f";[vcat]tpad=stop_mode=clone:stop_duration={tail:.3f}[v]"
    if not single_full_audio:
        filtergraph += ";" + f"[{n}:a]" + _audio_tail_filter(video_len)

    cmd += [
        "-filter_complex", filtergraph,
        "-map", "[v]",
        # при полном звуке берём дорожку как есть, без фильтра обрезки/затухания
        "-map", f"{n}:a" if single_full_audio else "[a]",
        "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
        "-shortest", out_path,
    ]
    subprocess.run(cmd, capture_output=True,
                         timeout=FFMPEG_TIMEOUT)
    return out_path


def _build_slideshow_mixed(items: list[tuple[str, bool]], audio: str, out_path: str,
                           still_sec: float = SLIDE_SEC) -> str:
    """Собирает видео из смешанных элементов: статичный кадр показывается still_sec
    секунд, «живой» кадр идёт своим коротким видео. Музыка — фоном, зациклена под всю
    длину и гасится в конце. Всё приводится к холсту 1080x1920.

    Одно статичное фото — особый случай: трек играет целиком, кадр висит всю его длину."""
    n = len(items)

    # Единственный статичный кадр: резать музыку нечего, показываем под неё целиком
    single_full_audio = n == 1 and not items[0][1]
    if single_full_audio:
        still_sec = _media_duration(audio) or still_sec

    cmd = [_ffbin("ffmpeg"), "-y"]
    video_len = 0.0
    for path, is_video in items:
        if is_video:
            cmd += ["-i", path]                                  # клип своей длины
            video_len += _media_duration(path) or still_sec
        else:
            cmd += ["-loop", "1", "-t", f"{still_sec:.3f}", "-i", path]
            video_len += still_sec
    if single_full_audio:
        cmd += ["-i", audio]                                     # звук целиком
    else:
        cmd += ["-stream_loop", "-1", "-i", audio]               # музыка (вход n), зациклена

    # Хвост под затухание: держим последний кадр ещё tail секунд, чтобы музыка
    # гасла ПОСЛЕ последнего слайда, не укорачивая его (см. _build_slideshow).
    tail = 0.0 if single_full_audio else max(SLIDE_AUDIO_FADE_SEC, 0.0)
    video_len += tail

    parts = []
    for i in range(n):
        parts.append(
            f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{i}]"
        )
    vlabel = "[vcat]" if tail > 0 else "[v]"
    concat = "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0{vlabel}"
    filtergraph = ";".join(parts) + ";" + concat
    if tail > 0:
        filtergraph += f";[vcat]tpad=stop_mode=clone:stop_duration={tail:.3f}[v]"
    if not single_full_audio:
        filtergraph += ";" + f"[{n}:a]" + _audio_tail_filter(video_len)

    cmd += [
        "-filter_complex", filtergraph,
        "-map", "[v]",
        # при полном звуке берём дорожку как есть, без обрезки/затухания
        "-map", f"{n}:a" if single_full_audio else "[a]",
        "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
        "-shortest", out_path,
    ]
    subprocess.run(cmd, capture_output=True,
                         timeout=FFMPEG_TIMEOUT)
    return out_path


def _is_short(url: str) -> bool:
    """Короткая ссылка-редирект TikTok (из кнопки «Поделиться»)."""
    return bool(url) and ("vt.tiktok.com" in url or "vm.tiktok.com" in url)


def _resolve_short(url: str) -> str:
    """Разворачивает короткую ссылку (vt./vm.tiktok.com) в полную.

    Читаем только заголовок Location, не скачивая страницу: обычный GET с переходами
    тянет весь HTML ради одного адреса и стоит ~0.8с против ~0.3с. Если по заголовкам
    не вышло — возвращаемся к обычному GET, а совсем не вышло — отдаём ссылку как есть.
    """
    if not _is_short(url):
        return url
    # По переходам идём САМИ и на каждом шаге проверяем, что следующий адрес — всё
    # ещё TikTok. Иначе короткая ссылка становится указателем куда угодно: раньше
    # достаточно было, чтобы «tiktok.com» встретилось в строке (хоть в параметре
    # запроса), и бот честно шёл по цепочке на любой чужой сайт.
    try:
        target = url
        for _ in range(5):                  # цепочка переходов бывает не одношаговой
            nxt = requests.get(target, headers=HEADERS, timeout=15,
                               allow_redirects=False).headers.get("Location")
            if not nxt:
                break
            nxt = requests.compat.urljoin(target, nxt)
            if not _is_tiktok_url(nxt):
                logger.warning("Короткая ссылка ведёт не на TikTok — не иду: %s",
                               nxt[:80])
                return url
            target = nxt
        if target != url and _is_tiktok_url(target):
            return target
    except Exception:
        pass
    try:
        # Запасной путь: здесь по переходам идёт requests, поэтому проверяем, КУДА
        # в итоге пришли, и чужой адрес не возвращаем.
        final = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True).url
        return final if _is_tiktok_url(final) else url
    except Exception:
        return url


def _is_tiktok_url(url: str) -> bool:
    """Ссылка действительно ведёт на TikTok (хозяин адреса, а не «есть в строке»)."""
    from bot.utils.platform_detector import Platform, detect_platform
    return detect_platform(url) == Platform.TIKTOK


def _api_call(url: str, hd: bool = True) -> dict:
    return _api_call_retry(url, hd=hd)


def _api_call_retry(url: str, attempts: int = 3, delay: float = 1.2,
                    hd: bool = True) -> dict:
    """Запрос к API TikTok (tikwm) с ПОВТОРАМИ. Под нагрузкой сервис иногда отдаёт
    пустой/битый ответ (тогда падает .json()), таймаут или ошибочный код — всё это
    временно. Пробуем до `attempts` раз с паузой. Если все попытки мимо — возвращаем
    словарь с code=-1 (msg=причина), чтобы верхний уровень пошёл в запасной сервис."""
    last = None
    for i in range(attempts):
        try:
            data = _via_json(requests.get, API, params={"url": url, "hd": 1 if hd else 0},
                             headers=HEADERS, timeout=30)
            if data.get("code") == 0:
                return data                      # успех
            last = data.get("msg") or f"code={data.get('code')}"
        except Exception as e:                   # пустой/битый ответ, сеть, таймаут
            last = f"{type(e).__name__}: {e}"
        if i < attempts - 1:
            time.sleep(delay)
    return {"code": -1, "msg": last or "TikTok API error"}


def _fetch_backup(url: str) -> dict | None:
    """Запасной сервис (lovetik): когда основной не отдал видео. Возвращает данные
    в том же формате, что и основной (kind='video'), или None. Слайдшоу не умеет."""
    try:
        j = _via_json(requests.post, BACKUP_API, data={"query": url},
                      headers=HEADERS, timeout=25)
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


def fetch_tiktok(url: str, hd: bool = True) -> dict:
    """Данные поста с коротким кэшем (TTL): один и тот же пост в рамках запроса
    (основной контент + аудиодорожка) не дёргает API дважды.

    hd=False — не просить HD-вариант. Сервис готовит его дольше (замер: 1.31с против
    1.04с), а при включённом «Сжатии шортс» мы всё равно берём обычное качество, так
    что просить HD означает просто ждать лишнее.
    """
    now = time.time()
    key = url if hd else f"{url}#sd"    # SD и HD-ответы различаются, не смешиваем
    # Кэш читают и пишут РАЗНЫЕ потоки (скачивания идут через asyncio.to_thread), а
    # уборка перебирает словарь целиком — без замка это «dictionary changed size
    # during iteration» раз в сто запросов, то есть случайный необъяснимый сбой.
    with _CACHE_LOCK:
        hit = _FETCH_CACHE.get(key)
    if hit and now - hit[0] < _FETCH_TTL:
        return hit[1]
    result = _fetch_tiktok_api(url, hd)  # успех или исключение (ошибки не кэшируем)
    with _CACHE_LOCK:
        _FETCH_CACHE[key] = (now, result)
        if len(_FETCH_CACHE) > 64:      # лёгкая уборка протухших записей
            for k in [k for k, (ts, _) in list(_FETCH_CACHE.items())
                      if now - ts >= _FETCH_TTL]:
                _FETCH_CACHE.pop(k, None)
    return result


def _fetch_tiktok_api(url: str, hd: bool = True) -> dict:
    """Запрашивает данные поста (без скачивания файлов) и определяет тип:
    'video' — обычное видео, 'slideshow' — набор фото (+ возможно музыка),
    'live' — Live Photo (короткие видео). Возвращает {'id','kind','data'}."""
    # Короткую ссылку сначала скармливаем API как есть: он их понимает, а разворот стоит
    # лишних ~0.8с на каждом видео. Одна попытка без пауз; не вышло — разворачиваем и
    # идём обычным путём с повторами, так что надёжность не теряем.
    payload = {}
    if _is_short(url):
        payload = _api_call_retry(url, attempts=1, hd=hd)
    if payload.get("code") != 0:
        url = _resolve_short(url)
        payload = _api_call(url, hd)  # внутри до 3 попыток (см. _api_call_retry)
    if payload.get("code") == 0:
        data = payload["data"]
        # Фото-посты (photo mode) содержат images. У «живых фото» вдобавок бывает
        # live_images с короткими видео, причём часть элементов может быть null.
        # Поэтому наличие images важнее: это полноценное слайдшоу (фото + музыка),
        # его мы всегда умеем отдать. Чистый Live Photo (только live_images) — отдельно.
        if data.get("images"):
            kind = "slideshow"
        elif data.get("live_images") and any(data["live_images"]):
            kind = "live"
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
        path = _fetch_file(img_url, os.path.join(
            DOWNLOADS_DIR, f"{item_id}_{i}_dl.jpg"), timeout=60)
        files.append(path)
    return files


def _download_slideshow_items(data: dict, item_id: str) -> list[tuple[str, bool]]:
    """Качает элементы слайдшоу в исходном порядке: живой кадр (есть live_images) —
    как короткое видео .mp4, статичный — как фото .jpg. Возвращает [(путь, это_видео)].
    Так фото отправляется фото, а «живое фото» — видео (как у оригинала в TikTok)."""
    images = data.get("images") or []
    lives = data.get("live_images") or []
    out: list[tuple[str, bool]] = []
    for i, img_url in enumerate(images, 1):
        live = lives[i - 1] if i - 1 < len(lives) else None
        if live:
            path = _fetch_file(live, os.path.join(
                DOWNLOADS_DIR, f"{item_id}_{i}_dl.mp4"))
            is_video = True
        else:
            path = _fetch_file(img_url, os.path.join(
                DOWNLOADS_DIR, f"{item_id}_{i}_dl.jpg"), timeout=60)
            is_video = False
        out.append((path, is_video))
    return out


def download_from(info: dict, mode: str = "auto", compress: bool = False) -> list[str]:
    """
    Скачивает TikTok по уже полученным данным (fetch_tiktok).
    mode для слайдшоу: 'photos' — только фото, 'video' — собрать видео со звуком,
    'auto' — видео, если есть музыка, иначе фото. Возвращает список файлов.
    compress — «сжатие шортс»: для ОБЫЧНОГО видео берём облегчённую версию поста.
        На слайдшоу не влияет: фото отдаются как есть, а видео из слайдшоу мы собираем
        сами из картинок, там выбирать нечего.
    """
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    data = info["data"]
    # Уникальный на КАЖДЫЙ вызов префикс имён файлов. Раньше имя было по номеру поста,
    # и два запроса одного слайдшоу (двойной тап по кнопке формата, «Видео»+«Фото»)
    # писали одни и те же файлы: один запрос доотправлял альбом и удалял файлы, пока
    # второй ещё грузил их в Telegram → FileNotFoundError на sendMediaGroup. item_id тут
    # используется ТОЛЬКО как имя файла, поэтому добавить случайный суффикс безопасно.
    item_id = f"{info['id']}_{uuid.uuid4().hex[:8]}"

    def _remember(paths):
        """Привязывает к скачанным файлам номер поста — по нему строится имя при
        отправке. Названия у TikTok нет: приходит подпись автора, а она бывает пустой,
        из одних хештегов или из одних эмодзи. Номер есть всегда."""
        for pth in paths if isinstance(paths, (list, tuple)) else [paths]:
            if pth:
                media_names.remember(pth, None, str(info.get("id") or ""))
        return paths

    # Live Photo: набор коротких видео — отдаём альбомом видео. Часть элементов
    # live_images бывает null (для статичных кадров) — их пропускаем.
    if info["kind"] == "live":
        files = []
        for i, vid_url in enumerate(data["live_images"], 1):
            if not vid_url:
                continue
            path = _fetch_file(vid_url, os.path.join(
                DOWNLOADS_DIR, f"{item_id}_{i}_dl.mp4"))
            files.append(path)
        return _remember(files)

    # Слайдшоу (фото-пост, возможно с «живыми фото»)
    if info["kind"] == "slideshow":
        has_live = any(data.get("live_images") or [])

        # Формат «фото»: отдаём как в оригинале — статичные кадры фото, живые видео.
        if mode == "photos":
            return _remember([p for p, _ in _download_slideshow_items(data, item_id)])

        # Формат «видео»/«авто»: собираем один ролик с музыкой. Если есть живые кадры —
        # с их движением (mixed), иначе обычное слайдшоу из фото.
        music_url = data.get("music")
        if has_live:
            items = _download_slideshow_items(data, item_id)
            all_files = [p for p, _ in items]
            if music_url:
                try:
                    audio_path = os.path.join(DOWNLOADS_DIR, f"{item_id}_audio.mp3")
                    _fetch_file(music_url, audio_path, timeout=60)
                    video_path = os.path.join(DOWNLOADS_DIR, f"{item_id}_dl.mp4")
                    _build_slideshow_mixed(items, audio_path, video_path)
                    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
                        for f in all_files:
                            _safe_remove(f)
                        _safe_remove(audio_path)
                        return _remember([video_path])
                    _safe_remove(audio_path)
                except Exception:
                    pass  # не вышло собрать — отдадим смешанным альбомом
            return _remember(all_files)

        files = _download_images(data["images"], item_id)
        if music_url:
            try:
                audio_path = os.path.join(DOWNLOADS_DIR, f"{item_id}_audio.mp3")
                _fetch_file(music_url, audio_path, timeout=60)
                video_path = os.path.join(DOWNLOADS_DIR, f"{item_id}_dl.mp4")
                _build_slideshow(files, audio_path, video_path)
                if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
                    for f in files:
                        _safe_remove(f)
                    _safe_remove(audio_path)
                    return _remember([video_path])
                _safe_remove(audio_path)
            except Exception:
                pass  # не вышло собрать видео — отдадим картинки
        return _remember(files)  # музыки нет (или сборка не удалась) — отдаём фото

    # Обычное видео. При «сжатии шортс» берём облегчённую версию (play) вместо HD:
    # она примерно вдвое легче при том же ролике и качается не медленнее — замерено.
    # Оба варианта лежат на CDN самого TikTok, водяного знака нет ни там, ни там.
    play = (data.get("play") or data.get("hdplay")) if compress else \
           (data.get("hdplay") or data.get("play"))
    path = _fetch_file(play, os.path.join(DOWNLOADS_DIR, f"{item_id}_dl.mp4"))
    return _remember([path])
