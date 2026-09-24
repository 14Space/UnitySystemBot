import os
import re
import copy
import time
import uuid
import glob
import logging
import yt_dlp

from bot.config import (
    DOWNLOADS_DIR, POT_PROVIDER_URL, YTDLP_CONCURRENT_FRAGMENTS, YOUTUBE_COOKIES,
)
from bot.utils import media_names, net, ffmpeg
from bot.utils import cookie_files
from bot.utils.limits import MAX_FILE_BYTES
from bot.utils.platform_detector import PINTEREST_DOMAINS

logger = logging.getLogger(__name__)


def _unique_outtmpl(suffix: str = "dl") -> str:
    """Шаблон имени файла, уникальный для КАЖДОГО скачивания.

    Раньше имя строилось только из номера ролика, и два скачивания одного и того же
    видео писали в одни и те же файлы: одно удаляло их за собой, второе в этот момент
    склеивало и падало с «No such file or directory» либо «Invalid data». Ловится это
    легко (два пункта проверки на одну ссылку), а у пользователей выглядело бы как
    случайный сбой раз в сто запросов.

    yt-dlp к тому же качает видео и звук отдельными файлами и склеивает третьим, так
    что столкнуться можно и на промежуточных кусках.
    """
    return os.path.join(DOWNLOADS_DIR, f"%(id)s_{suffix}_{uuid.uuid4().hex[:8]}.%(ext)s")


# Площадки, которые бот качает через yt-dlp (регулярки по имени извлекателя, регистр
# не важен). TikTok – ради аудиодорожки, если tikwm промолчит.
ALLOWED_EXTRACTORS = ["youtube.*", "soundcloud.*", "pornhub.*", "pinterest.*",
                      "instagram.*", "tiktok.*"]

# Номер ролика от площадки идёт прямо в имя файла. У настоящих номеров только буквы,
# цифры, дефис и подчёркивание; «../» в номере означал бы запись в чужую папку.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _safe_id(value, default: str = "media") -> str:
    text = str(value or "")
    return text if _SAFE_ID_RE.match(text) else default


# Короткие ссылки, у которых в yt-dlp нет своего извлекателя: раньше по ним шёл
# «generic», а он выключен (см. ALLOWED_EXTRACTORS). Короткий хост -> куда он вправе вести.
_SHORT_HOSTS = {
    "pin.it": PINTEREST_DOMAINS,
    "on.soundcloud.com": ("soundcloud.com",),
}


def resolve_short(url: str) -> str:
    """Разворачивает короткую ссылку площадки в полную (остальные – как есть).

    Зовётся в КАЖДОЙ точке входа этого модуля, а не у вызывающих: ссылка приходит и от
    человека, и из проверки функционала, и из inline-режима. 24.09.2026 разворот стоял
    только в обработчике сообщений – и проверка «SoundCloud трек» на короткой ссылке
    покраснела: yt-dlp её не узнал, а запасной поиск на YouTube искал хвост ссылки.
    Каждый переход проверяется ДО того, как по нему идти (net.follow_redirects).
    """
    for host, targets in _SHORT_HOSTS.items():
        if net.url_on(url, host):
            return net.follow_redirects(url, host, *targets)
    return url

BASE_OPTS = {
    "quiet": True,
    # Видео у YouTube (и почти везде, где есть выбор качества) лежит не одним файлом,
    # а сотнями кусков. По умолчанию yt-dlp берёт их строго по одному: попросил кусок —
    # дождался — попросил следующий. Основное время при этом уходит не на скачивание,
    # а на ожидание ответа, и через наш туннель (запрос идёт до дома владельца и
    # обратно) это ожидание особенно дорогое. Четыре куска разом закрывают паузы друг
    # друга. Больше ставить не стоит: на несколько одновременных загрузок это
    # умножается, и площадка начинает отвечать ошибками «слишком часто».
    "concurrent_fragment_downloads": YTDLP_CONCURRENT_FRAGMENTS,
    # Кусок иногда не доезжает сам по себе (обрыв, 5xx). Это не повод терять весь
    # ролик: повторяем именно его.
    "fragment_retries": 5,
    "no_warnings": True,  # глушим предупреждения yt-dlp (n challenge и т.п.) — лог чистый
    # Гасим текстовую полоску прогресса yt-dlp в stderr (в логах она сыплется сотнями
    # строк «[download] 45.2% of…», особенно при проверке функционала). Прогресс, который
    # видит пользователь в Telegram, идёт отдельно через progress_hooks и не затрагивается.
    "noprogress": True,
    # Клиент НЕ переопределяем: набор по умолчанию у ночной сборки yt-dlp сам выбирает
    # рабочие форматы (в т.ч. через SABR — новый протокол YouTube). Пропуски берём у
    # POT-провайдера (без его пропусков YouTube отдаёт 403 на скачивание) — вместе
    # это снимает 403 на популярных роликах, Shorts и YT Music.
    "extractor_args": {
        "youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]},
    },
    # Потолок размера. Смысл не в диске, а в бессмысленной работе: файл больше 2 ГБ
    # Telegram всё равно не примет, а без этого предела бот честно качал его целиком
    # (часами, через домашний канал) — и только потом отвечал «слишком большой».
    "max_filesize": MAX_FILE_BYTES,
    # Только извлекатели НАШИХ площадок. По умолчанию yt-dlp включает и «generic» –
    # он открывает любую страницу и качает то, на что она укажет. Вместе с поддельным
    # доменом это давало боту команду «скачай такой-то адрес внутренней сети» (проверено
    # на деле при повторном аудите 24.09.2026). Поиск (ytsearch/scsearch) сюда тоже
    # входит: это извлекатели YoutubeSearch и SoundcloudSearch.
    "allowed_extractors": ALLOWED_EXTRACTORS,
}
if ffmpeg.FFMPEG_DIR:
    BASE_OPTS["ffmpeg_location"] = ffmpeg.FFMPEG_DIR

# Прокси (PROXY_URL) — обход блокировок YouTube/YT Music (репутация IP сервера у
# YouTube — «Sign in to confirm you're not a bot») и PornHub (блок целой страны).
# Сам прокси, особенно домашний, не такой быстрый, как сервер, поэтому сначала идём
# напрямую и лишь при отказе повторяем через прокси (см. net.with_proxy). Исключение —
# PornHub: там блок ПО СТРАНЕ целиком, прямой заход обречён заранее.


def _needs_proxy(url: str) -> bool:
    """Площадки, где наш серверный IP может быть заблокирован/на подозрении.

    «ytsearch» — это не ссылка, а запрос поиска по YouTube (так качаются треки
    Spotify и запасной путь SoundCloud). Его сюда пришлось добавить отдельно: строка
    вида «ytsearch8:Ed Sheeran Shape of You» не ссылка вовсе, поэтому поиск шёл мимо
    прокси и упирался в тот же бот-чек, от которого прокси и спасает. Ссылки чинились,
    а поиск — нет, и ломались ровно Spotify и SoundCloud.
    """
    u = url or ""
    return u.startswith("ytsearch") or net.url_on(u, "youtube.com", "youtu.be", "pornhub.com")


def _proxy_opts(proxy: str) -> dict:
    """Опции yt-dlp для захода через прокси. Через туннель данные идут медленнее —
    стандартных 20с yt-dlp не хватает, поэтому ждём дольше."""
    return {"proxy": proxy, "socket_timeout": net.PROXY_SOCKET_TIMEOUT} if proxy else {}


def via_proxy(url: str, op):
    """op(proxy_opts: dict) -> результат, с откатом на прокси там, где он нужен.

    Прокси не задан или площадке он не нужен — один прямой вызов. PornHub — сразу
    через прокси (страновой блок прямой заход не переживёт). YouTube/YT Music —
    сначала напрямую, при ошибке повтор через прокси (или сразу, при PROXY_FIRST).
    """
    if not _needs_proxy(url):
        return op({})
    first = True if net.url_on(url, "pornhub.com") else None
    return net.with_proxy(lambda proxy: op(_proxy_opts(proxy)), net.proxy_for(), first=first)


# PornHub спрятан за Cloudflare: обычный запрос ловит 403. Маскируемся под настоящий
# Chrome (yt-dlp impersonate; работает благодаря пакету curl_cffi). Готовим цель один
# раз; если curl_cffi нет (запуск без Docker) — тихо пропускаем, будет как раньше.
try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
    _CHROME_TARGET = ImpersonateTarget.from_str("chrome")
except Exception:
    _CHROME_TARGET = None


def youtube_search_query(url: str) -> str:
    """Поисковый запрос «Исполнитель Название» по ссылке YouTube или YT Music.

    Нужен, когда сам ролик недоступен: лейбловые релизы в YT Music часто отдают «Video
    unavailable» — запись снял правообладатель. Трек при этом обычно лежит на обычном
    YouTube другой загрузкой, и найти его можно по названию.

    Данные берём у ОТКРЫТОГО метода самого YouTube (oembed): он отвечает даже по тем
    роликам, которые yt-dlp уже не открывает, не требует ключей и сторонних сервисов.

    У автоматических каналов исполнителей YouTube приписывает к имени «- Topic» —
    в поисковом запросе она только мешает, поэтому срезаем.
    """
    try:
        r = net.session().get("https://www.youtube.com/oembed",
                              params={"url": url, "format": "json"},
                              proxies=net.as_requests(net.proxy_for()), timeout=20)
        data = r.json()
    except Exception:
        logger.info("Не удалось прочитать данные ролика для поиска: %s", url)
        return ""
    author = re.sub(r"\s*-\s*Topic$", "", (data.get("author_name") or "").strip())
    title = (data.get("title") or "").strip()
    return " ".join(x for x in (author, title) if x)


def _cookie_opts(url: str, proxy_opts: dict | None = None) -> dict:
    """Куки YouTube — только для самого YouTube и поиска по нему.

    Нужны ради роликов с возрастным ограничением: без входа yt-dlp отвечает «Sign in to
    confirm your age», и ролик не скачивается вовсе. Обычные видео идут и без кук.

    Отдаём ПОСТОЯННУЮ рабочую копию, а не одноразовую. Разница принципиальная и
    выяснилась на практике: Google ротирует куки — на каждый запрос присылает свежий
    токен взамен старого, и через несколько часов прежний перестаёт приниматься.
    Одноразовая копия эти обновления выбрасывала, и сессия «протухала» сама собой,
    хотя срок у кук до 2027 года. Оригинал при этом не трогаем: он лежит как
    исходник на случай, когда рабочую копию надо завести заново.

    (У Instagram задача обратная — там сервер присылает УРЕЗАННЫЙ набор и затирает
    ключ входа, поэтому ему по-прежнему достаётся одноразовая копия.)

    Живёт копия РЯДОМ С ОРИГИНАЛОМ, а не в папке загрузок: ту вычищают при каждом
    старте бота, и вместе с хвостами загрузок улетала вся накопленная ротация —
    после любого деплоя мы шли с выгрузки многодневной давности.

    Другим площадкам куки YouTube не отдаём: это чужая учётная запись, ей незачем
    уезжать на PornHub или SoundCloud вместе с запросом.

    И главное: куки уходят ТОЛЬКО через дом. Если попытка идёт напрямую с адреса
    сервера, файл кук не прикладываем вовсе — возрастной ролик тогда не скачается, но
    вход останется целым. Раньше это держалось только на настройке PROXY_FIRST: с ней
    прямых попыток по YouTube не бывает, а без неё первая попытка шла напрямую и несла
    сессию в дата-центр. Ровно так мы уже потеряли сессии Instagram и Google.
    """
    u = url or ""
    if not YOUTUBE_COOKIES:
        return {}
    if not (u.startswith("ytsearch") or net.url_on(u, "youtube.com", "youtu.be")):
        return {}
    if net.proxy_for() and not (proxy_opts or {}).get("proxy"):
        return {}                    # прямая попытка — идём БЕЗ кук
    copy = cookie_files.working(YOUTUBE_COOKIES,
                                os.path.dirname(YOUTUBE_COOKIES) or ".")
    return {"cookiefile": copy} if copy else {}


def _impersonate_opts(url: str) -> dict:
    """Маскировку под браузер включаем ТОЛЬКО для PornHub (обход Cloudflare 403)."""
    if _CHROME_TARGET is not None and net.url_on(url, "pornhub.com"):
        return {"impersonate": _CHROME_TARGET}
    return {}


def download_probe(url: str, audio_only: bool = False) -> str:
    """Качает САМЫЙ ЛЁГКИЙ формат ролика — для проверки «скачивание работает» без траты
    трафика на полное качество. Проходит тот же реальный путь, что и боевое скачивание
    (POT-токены, маскировка под браузер, прокси), поэтому ловит те же поломки. Никакой
    пост-обработки (перекодирование/теги) — только байты. Возвращает путь к файлу."""
    url = resolve_short(url)
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    tag = uuid.uuid4().hex[:8]
    # Видео: «worstvideo*+worstaudio/worst» — самое лёгкое видео+звук, иначе самый лёгкий
    # единый формат. Просто «worst» у YouTube ловит SABR («формат недоступен»), поэтому так.
    fmt = "worstaudio/worst" if audio_only else "worstvideo*+worstaudio/worst"

    def _op(proxy_opts: dict) -> str:
        opts = {
            **BASE_OPTS,
            "format": fmt,
            "outtmpl": os.path.join(DOWNLOADS_DIR, f"probe_{tag}_%(id)s.%(ext)s"),
            "postprocessors": [],
            "noplaylist": True,
            **proxy_opts,
            **_impersonate_opts(url),
            **_cookie_opts(url, proxy_opts),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = glob.glob(os.path.join(DOWNLOADS_DIR, f"probe_{tag}_*"))
        return files[0] if files else ""

    return via_proxy(url, _op)


def get_video_info(url: str, allow_drm: bool = False) -> dict:
    """Получает информацию о видео без скачивания.
    allow_drm=True — не падать на DRM-треках, а вернуть метаданные (название, длительность)
    без самих форматов. Нужно, чтобы по названию найти трек на YouTube."""
    url = resolve_short(url)
    def _op(proxy_opts: dict) -> dict:
        opts = dict(BASE_OPTS)
        if allow_drm:
            opts["ignore_no_formats_error"] = True
        opts.update(proxy_opts)  # для YT Music: пусто напрямую, затем прокси при неудаче
        opts.update(_impersonate_opts(url))  # маскировка под Chrome только для PornHub
        opts.update(_cookie_opts(url, proxy_opts))   # куки YouTube — ради возрастных роликов
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    return via_proxy(url, _op)


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


def estimate_size(info: dict, quality: int) -> int | None:
    """Сколько примерно весит ролик в этом качестве, байт (None — оценить нечем).

    Зачем: у Telegram потолок 2 ГБ, и раньше мы узнавали о превышении ТОЛЬКО после
    полной загрузки — то есть уже потратив полчаса и гигабайты домашнего канала на
    файл, который всё равно не уйдёт. Площадка обычно сообщает размер заранее, так
    что честнее предупредить до начала.

    Считаем как и качаем: лучшая видеодорожка нужной высоты плюс лучшая звуковая.
    Точного размера не обещаем (у части форматов он лишь приблизительный) — поэтому
    вызывающий код использует оценку только как повод предупредить, а не как запрет.
    """
    video, audio = 0, 0
    for fmt in info.get("formats") or []:
        size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        if not size:
            continue
        vcodec = fmt.get("vcodec", "none")
        acodec = fmt.get("acodec", "none")
        height = fmt.get("height")
        if vcodec != "none" and height and _snap_to_standard(height) == quality:
            video = max(video, size)
            if acodec != "none":          # формат «всё в одном» — звук уже внутри
                audio = max(audio, 0)
        elif vcodec == "none" and acodec != "none":
            audio = max(audio, size)
    if not video:
        return None
    return video + audio


def download_video(
    url: str,
    quality: int,
    progress_callback=None,
    postprocess_callback=None,
    info: dict | None = None,
) -> str:
    """
    Скачивает видео в указанном качестве.
    info — уже полученные метаданные (чтобы не запрашивать площадку второй раз): их
        достали, когда показывали кнопки качества. Экономит повторный поход в сеть.
    progress_callback(percent) — вызывается во время скачивания.
    postprocess_callback() — вызывается когда ffmpeg начинает склейку.
    """
    url = resolve_short(url)
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    # Имя УНИКАЛЬНОЕ на каждое скачивание: два человека, качающие одно видео в одном
    # качестве, писали в один файл — первый удалял его за собой, второй падал на
    # склейке или отправлял пустоту (см. _unique_outtmpl).
    output_path = _unique_outtmpl(f"{quality or 'best'}p")

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

    def _op(proxy_opts: dict) -> str:
        ydl_opts = {
            **BASE_OPTS,
            "format": fmt,
            "outtmpl": output_path,
            "merge_output_format": "mp4",
            "progress_hooks": [progress_hook],
            # +faststart переносит метаданные в начало файла — видео играется на лету,
            # не дожидаясь полной загрузки на стороне зрителя
            "postprocessor_args": {"merger": ["-movflags", "+faststart"]},
            **proxy_opts,  # для YouTube/PornHub: пусто напрямую, затем прокси при неудаче
            **_impersonate_opts(url),
            **_cookie_opts(url, proxy_opts),   # куки YouTube — ради возрастных роликов
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                extracted = _extract_for_download(ydl, url, info)
                filename = ydl.prepare_filename(extracted)
                if not os.path.exists(filename):
                    filename = filename.rsplit(".", 1)[0] + ".mp4"
                media_names.remember(filename, extracted.get("title"), extracted.get("id"))
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

    return via_proxy(url, _op)


def _extract_for_download(ydl, url: str, info: dict | None):
    """Готовит метаданные для скачивания. Если они УЖЕ получены раньше (когда показывали
    кнопки качества) — переиспользуем их и не ходим к площадке второй раз: это экономит
    ~1.5с на каждом видео. Если переиспользовать не вышло (ссылки формата протухли, другой
    extractor и т.п.) — честно извлекаем заново, чтобы скачивание точно не сломалось."""
    if info:
        try:
            return ydl.process_video_result(copy.deepcopy(info), download=True)
        except Exception:
            logger.info("Метаданные переиспользовать не вышло — извлекаю заново", exc_info=True)
    return ydl.extract_info(url, download=True)


# Кодеки, которые iPhone/Telegram на iOS играют аппаратно. Остальное (vp9, av01) —
# чёрный экран при живом звуке, поэтому перекодируем в h264.
_IOS_OK_CODECS = ("h264", "avc1", "hevc", "h265")


def _video_codec(path: str) -> str:
    """Имя видеокодека файла через ffprobe (пустая строка, если не удалось)."""
    try:
        out = ffmpeg.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", path],
            text=True,
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

    out_path = path.rsplit(".", 1)[0] + "_h264.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", path,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        out_path,
    ]
    res = ffmpeg.run(cmd)
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
    term = f"ytsearch{count}:{query}"

    def _op(proxy_opts: dict):
        with yt_dlp.YoutubeDL({**BASE_OPTS, "noplaylist": True, **proxy_opts}) as ydl:
            return ydl.extract_info(term, download=False)

    res = via_proxy(term, _op)

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
    term = f"ytsearch{count}:{query}"

    def _op(proxy_opts: dict):
        with yt_dlp.YoutubeDL({**BASE_OPTS, "noplaylist": True, **proxy_opts}) as ydl:
            return ydl.extract_info(term, download=False)

    res = via_proxy(term, _op)
    entries = [e for e in (res.get("entries") or []) if e]
    if target_duration:
        entries.sort(key=lambda e: abs((e.get("duration") or 0) - target_duration))
    return [e.get("webpage_url") or f"https://www.youtube.com/watch?v={e['id']}" for e in entries]


def get_soundcloud_set(url: str) -> dict:
    """Читает альбом/плейлист (set) SoundCloud: название и список треков с реальными именами.
    Полное чтение (не flat) — иначе у части треков вместо названия числовой id.
    ignore_no_formats_error — чтобы DRM-треки тоже отдавали название."""
    url = resolve_short(url)
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
    url = resolve_short(url)
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    # По названию трека имя не строим: у двух запросов одного трека он совпадал, а
    # длинные кириллические названия давали «File name too long». Настоящее имя для
    # человека всё равно ставится при отправке (см. media_names).
    output_path = _unique_outtmpl("audio")

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

    def _op(proxy_opts: dict) -> str:
        ydl_opts = {
            **BASE_OPTS,
            "format": "bestaudio/best",
            "outtmpl": output_path,
            "progress_hooks": [progress_hook],
            "writethumbnail": embed_thumbnail,  # обложку источника качаем только если вшиваем
            "postprocessors": postprocessors,
            **proxy_opts,  # для YT Music: пусто напрямую, затем прокси при неудаче
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            extracted = ydl.extract_info(url, download=True)
            # ytsearch (Spotify) возвращает "плейлист" — берём первый реальный трек
            if "entries" in extracted:
                extracted = extracted["entries"][0]
            filename = ydl.prepare_filename(extracted)
            # «Исполнитель – Трек», если исполнитель известен; иначе просто название.
            # Тире именно среднее (–), как в подписях у площадок.
            artist = extracted.get("artist") or extracted.get("uploader") or ""
            track = extracted.get("track") or extracted.get("title") or ""
            nice = f"{artist} – {track}" if artist and track else (track or artist)
            # после конвертации исходное расширение (webm/m4a) заменяется на mp3
            mp3_path = filename.rsplit(".", 1)[0] + ".mp3"
            if os.path.exists(mp3_path):
                media_names.remember(mp3_path, nice, extracted.get("id"))
                return mp3_path
            media_names.remember(filename, nice, extracted.get("id"))
            return filename

    return via_proxy(url, _op)


def download_media(url: str) -> str:
    """
    Универсальное скачивание одного медиа (TikTok, Pinterest): видео или фото.
    Возвращает путь к файлу (ext подскажет тип: mp4 — видео, jpg — фото).
    """
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    output_path = _unique_outtmpl()

    url = resolve_short(url)

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
            media_names.remember(filename, info.get("title"), info.get("id"))
            return filename

        # Видео нет (фото-пин) — качаем картинку напрямую, СОХРАНЯЯ реальное расширение
        # (важно для .gif — иначе анимация теряется и шлётся как статичное фото)
        image_url = _best_image_url(info)
        if image_url:
            ext = os.path.splitext(image_url.split("?")[0])[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                ext = ".jpg"
            # Имя уникальное (два запроса одного пина не делят файл), номер проверен.
            path = os.path.join(DOWNLOADS_DIR, f"{_safe_id(info.get('id'))}_dl_"
                                               f"{uuid.uuid4().hex[:8]}{ext}")
            net.fetch_to_file(image_url, path, timeout=60)
            return path

        raise ValueError("В этом пине нет медиа")


def convert_gif_to_mp4(gif_path: str) -> str:
    """Конвертирует GIF в чистый mp4 (H.264) — чтобы Telegram не пере-сжимал грубо.
    Если не вышло — возвращает исходный gif."""
    mp4 = gif_path.rsplit(".", 1)[0] + "_anim.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", gif_path,
        "-movflags", "faststart", "-pix_fmt", "yuv420p",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-crf", "18",
        mp4,
    ]
    ffmpeg.run(cmd)
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


# Порядок здесь решает всё. «best» означает «лучший файл, где видео и звук УЖЕ вместе»,
# а у YouTube такой ровно один — формат 18, 360x640. Пока он стоял первым, цепочка на нём
# и останавливалась: шортс приходил в 360p при доступных 1080p. Получить максимум удавалось
# лишь случайно — когда YouTube этот формат не предлагал и цепочка проваливалась дальше.
# Отсюда были и скачки качества от запроса к запросу, и краснеющие пункты проверки.
#
# Поэтому сперва раздельные видео+звук (их yt-dlp склеит через ffmpeg), и только последним
# рубежом — готовый файл: он нужен площадкам вроде Instagram, где раздельных дорожек нет.
_FMT_CHAIN = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"


def _quality_opts(max_h: int | None) -> dict:
    """Опции выбора качества. Без ограничения — максимум.

    С ограничением («Сжатие шортс») НЕ фильтруем по height: у вертикальных роликов высота
    1280 при ширине 720, поэтому «height<=720» отсекал всё кроме 360x640 и ронял качество
    вчетверо. Вместо фильтра сортируем по res (короткая сторона) — так 720 означает именно
    720p и для вертикальных, и для горизонтальных.

    Список форматов не сужаем: у площадок с единственным форматом (Instagram) строгий лимит
    уронил бы скачивание «формат недоступен», а сортировка безопасна — она лишь меняет
    порядок предпочтений.
    """
    if not max_h:
        return {"format": _FMT_CHAIN}
    return {"format": _FMT_CHAIN, "format_sort": [f"res:{max_h}"]}


def download_shorts(url: str, max_height: int | None = None) -> str:
    """
    Скачивает короткое видео (YouTube Shorts, PornHub Shorties).

    max_height — потолок качества («сжатие шортс»): None = максимальное.
    При сбое скачивания (частый случай — тяжёлый файл на 40+ МБ и медленный CDN, из-за
    чего рвётся соединение) автоматически повторяем в качестве пониже: лучше отдать
    ролик чуть менее чётким, чем не отдать совсем.
    """
    url = resolve_short(url)
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    output_path = _unique_outtmpl()

    # Лесенка попыток: запрошенное качество, затем всё более лёгкие варианты.
    ladder = [max_height, 720, 480] if max_height else [None, 720, 480]
    seen, attempts = set(), []
    for h in ladder:                       # убираем дубли, сохраняя порядок
        if h not in seen:
            seen.add(h)
            attempts.append(h)

    def _op(proxy_opts: dict) -> str:
        last_err = None
        for i, h in enumerate(attempts):
            # На промежуточных попытках не терпим долгие залипания: если тяжёлый файл встал,
            # быстрее откатиться на качество пониже, чем ждать 25с ради максимума. На ПОСЛЕДНЕЙ
            # попытке возвращаем обычное терпение yt-dlp — сдаваться раньше времени нельзя.
            # Промежуточные попытки нарочно нетерпеливы: быстрее откатиться на качество
            # пониже, чем ждать. Но через прокси даже здоровая загрузка идёт медленнее,
            # и прежние 10с приводили к откату на ровном месте — поэтому порог зависит
            # от того, работаем мы напрямую или через туннель.
            quick = 10 if not proxy_opts.get("proxy") else max(10, net.PROXY_SOCKET_TIMEOUT // 2)
            impatient = {"socket_timeout": quick, "retries": 1} if i < len(attempts) - 1 else {}
            ydl_opts = {
                **BASE_OPTS,
                **_quality_opts(h),
                "outtmpl": output_path,
                "merge_output_format": "mp4",
                **impatient,
                **proxy_opts,  # для YouTube/PornHub: пусто напрямую, затем прокси при неудаче
                **_impersonate_opts(url),
                **_cookie_opts(url, proxy_opts),   # куки YouTube — ради возрастных роликов
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    extracted = ydl.extract_info(url, download=True)
                    filename = ydl.prepare_filename(extracted)
                    if not os.path.exists(filename):
                        filename = filename.rsplit(".", 1)[0] + ".mp4"
                    # Название нужно на отправке, чтобы файл пришёл человеку не под
                    # техническим именем; сюда оно доезжает только отсюда.
                    media_names.remember(filename, extracted.get("title"), extracted.get("id"))
                    return filename
            except Exception as e:
                last_err = e
                if i < len(attempts) - 1:
                    logger.info("Короткое видео не скачалось (%s) — пробую качество пониже (%sp)",
                                str(e)[:120], attempts[i + 1])
        raise last_err

    return via_proxy(url, _op)
