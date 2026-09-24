import functools
import logging
import os
import re
import uuid
import time
import HdRezkaApi.api as _hdrezka_api
from HdRezkaApi import HdRezkaApi
from bot.config import DOWNLOADS_DIR
from bot.features.download.downloaders import hdrezka_gate
from bot.utils import ffmpeg, files, net
from bot.utils.limits import FileTooLargeError, MAX_FILE_BYTES

logger = logging.getLogger(__name__)

# Коды языков субтитров → метка для контейнера mp4
_LANG = {"ru": "rus", "en": "eng", "ua": "ukr", "uk": "ukr"}


# --- Заплатка под новую разметку сайта ---------------------------------------
# HDRezka переехал: атрибут data-translator_id (id озвучки) раньше был на <li>,
# теперь — на вложенной ссылке <a> внутри <li>. Библиотека HdRezkaApi (11.2.3 —
# уже последняя) читает его со старого места и падает с KeyError. Подменяем её
# свойство `translators` на устойчивую версию: ищем ссылки с data-translator_id
# на любой глубине списка. Остальное (сезоны/серии) сайт не менял.
def _translators_patched(self):
    arr = {}
    tl = self.soup.find(id="translators-list")
    if tl:
        # Новое место id — на вложенной ссылке <a data-translator_id="…">
        for a in tl.find_all(attrs={"data-translator_id": True}):
            tid = int(a.attrs["data-translator_id"])
            name = a.text.strip()
            premium = "b-prem_translator" in (a.get("class") or [])
            img = a.find("img")
            if img:
                lang = img.attrs.get("title")
                if lang and lang not in name:
                    name += f" ({lang})"
            arr[tid] = {"name": name, "premium": premium}

    # Одноголосый фильм/сериал: списка озвучек нет — определяем единственную озвучку
    # из тела страницы (как в оригинале библиотеки; эту ветку сайт не менял).
    if not arr:
        def getTranslationName(s):
            table = s.find(class_="b-post__info")
            for tr in table.findAll("tr"):
                if "переводе" in tr.get_text():
                    td = tr.find_all("td")[-1]
                    return td.get_text().strip()

        def getTranslationID(s):
            initCDNEvents = {"video.tv_series": "initCDNSeriesEvents",
                             "video.movie": "initCDNMoviesEvents"}
            tmp = s.text.split(f"sof.tv.{initCDNEvents[f'video.{self.type.name}']}")[-1].split("{")[0]
            return int(tmp.split(",")[1].strip())

        arr[getTranslationID(self.page)] = {"name": getTranslationName(self.soup), "premium": False}
    return arr


_cp = functools.cached_property(_translators_patched)
_cp.__set_name__(_hdrezka_api.HdRezkaApi, "translators")
_hdrezka_api.HdRezkaApi.translators = _cp


def open_media(url: str) -> HdRezkaApi:
    """Создаёт объект HDRezka (одна загрузка страницы). Переиспользуем на всех шагах.

    Сайт закрыт анти-бот-проверкой Anubis — сначала добываем куки-пропуск через
    браузер (hdrezka_gate). Если сохранённые куки протухли (страница снова
    отдаёт заглушку и разбор падает), один раз проходим проверку заново."""
    headers = {"User-Agent": hdrezka_gate.USER_AGENT}
    for force in (False, True):
        cookies = hdrezka_gate.get_cookies(url, force=force)
        api = HdRezkaApi(url, headers=headers, cookies=cookies)
        try:
            _ = api.type      # триггерит разбор страницы; на заглушке кинет ошибку
            return api
        except Exception:
            if force:         # уже перепроходили проверку — не помогло, отдаём ошибку выше
                raise
            logger.info("HDRezka: куки не подошли, прохожу проверку заново")
    return api  # недостижимо, но для наглядности


def _clean_name(n: str | None) -> str:
    """Убираем «(+субтитры)» — в Telegram мягкие субтитры не видны, не путаем людей.
    У одноголосого контента имя озвучки может отсутствовать (None) — тогда даём
    нейтральную подпись «Оригинал»."""
    if not n:
        return "Оригинал"
    return n.replace(" (+субтитры)", "").replace(" (+subtitles)", "").strip()


def _retry(fn, tries: int = 3, delay: float = 1.5):
    """Повторяет операцию: первый AJAX-запрос к HDRezka после прохождения проверки
    иногда возвращает заглушку вместо JSON — повтор через паузу обычно проходит."""
    last = None
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            if attempt < tries - 1:
                logger.info("HDRezka: запрос не прошёл (%s), повтор %d/%d",
                            type(e).__name__, attempt + 2, tries)
                time.sleep(delay)
    raise last


def get_info(r: HdRezkaApi, url: str) -> dict:
    """Базовая инфа. Для фильма — сразу озвучки; для сериала — список сезонов."""
    is_series = "series" in str(r.type)
    info = {
        "name": r.name,
        "is_series": is_series,
        "thumbnail": getattr(r, "thumbnailHQ", None) or getattr(r, "thumbnail", None),
    }
    if is_series:
        # Список сезонов приходит AJAX-запросом — оборачиваем в повтор (см. _retry)
        info["seasons"] = [s["season"] for s in _retry(lambda: r.episodesInfo)]
    else:
        info["translators"] = [
            (tid, _clean_name(details["name"]))
            for tid, details in r.translators.items()
            if not details.get("premium")
        ]
    return info


def get_episodes(r: HdRezkaApi, season: int) -> list[int]:
    """Список серий в сезоне."""
    eps = next((s["episodes"] for s in r.episodesInfo if s["season"] == season), [])
    return [e["episode"] for e in eps]


def get_translators(r: HdRezkaApi, season: int, episode: int) -> list:
    """Озвучки конкретной серии (без премиум)."""
    eps = next((s["episodes"] for s in r.episodesInfo if s["season"] == season), [])
    ep = next((e for e in eps if e["episode"] == episode), None)
    raw = ep["translations"] if ep else []
    return [
        (t["translator_id"], _clean_name(t["translator_name"]))
        for t in raw if not t.get("premium")
    ]


def get_stream(r: HdRezkaApi, translation: int, season=None, episode=None):
    """Поток для озвучки (одна сетевая операция). Объект потока кэшируем и потом качаем из него."""
    return _retry(lambda: r.getStream(season=season, episode=episode, translation=translation))


def stream_qualities(stream) -> list[str]:
    """Список качеств у уже полученного потока."""
    return list(stream.videos.keys())


def _download_file(url: str, path: str, total: int = 0, progress_callback=None):
    """Потоком на диск, с пределом размера и полоской – общим загрузчиком (net)."""
    net.fetch_to_file(url, path, timeout=180, total=total, progress=progress_callback)


def _safe_rm(path: str):
    files.remove(path)


# Код языка субтитров: буквы, цифры и дефис, не длиннее десяти знаков.
_SUB_CODE_RE = re.compile(r"^[A-Za-z0-9-]{1,10}$")


def _mux_subtitles(video: str, subs: list, out_path: str) -> bool:
    """Вшивает субтитры мягкой дорожкой (mov_text) — без перекодирования видео."""
    cmd = ["ffmpeg", "-y", "-i", video]
    for _, sub_path in subs:
        cmd += ["-i", sub_path]
    cmd += ["-map", "0"]
    for i in range(1, len(subs) + 1):
        cmd += ["-map", str(i)]
    cmd += ["-c", "copy", "-c:s", "mov_text"]
    for idx, (code, _) in enumerate(subs):
        cmd += [f"-metadata:s:s:{idx}", f"language={_LANG.get(code, code)}"]
    # faststart — moov-атом в начало файла, иначе на iOS видео стримится чёрным экраном
    cmd += ["-movflags", "+faststart", out_path]
    res = ffmpeg.run(cmd)
    return res.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0


def download_stream(stream, quality: str, name="video", season=None, episode=None,
                    progress_callback=None) -> str:
    """Качает видео из уже полученного потока (без повторного запроса).
    Если у потока есть субтитры — тихо вшиваем их мягкими дорожками."""
    value = stream.videos[quality]
    video_url = value[0] if isinstance(value, (list, tuple)) else value

    # Заранее узнаём размер — чтобы не качать 80% и не упереться в лимит в конце.
    # Предел проверяет сам загрузчик (net.fetch_to_file), здесь – только размер для
    # полоски, если сервер назовёт его на HEAD и промолчит на GET.
    try:
        head = net.session().head(video_url, timeout=30, allow_redirects=True)
        size = int(head.headers.get("Content-Length", 0))
    except Exception:
        size = 0
    if size and size > MAX_FILE_BYTES:
        raise FileTooLargeError(f"{size / 1024 ** 3:.1f} ГБ")

    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    safe = re.sub(r"[^\w]+", "_", name)[:40] or "video"
    suffix = f"_s{season}e{episode}" if season and episode else ""
    # Хвост уникален для каждой загрузки. Без него имя складывалось только из названия
    # (качество в него даже не входило), и два человека, качающие один фильм — или один
    # фильм в двух качествах — писали в один и тот же файл. У HDRezka файлы на гигабайты,
    # то есть окно для столкновения — минуты, а результат: битое видео или падение.
    base = os.path.join(DOWNLOADS_DIR, f"{safe}{suffix}_dl_{uuid.uuid4().hex[:8]}")
    raw = base + "_raw.mp4"
    final = base + ".mp4"

    _download_file(video_url, raw, total=size, progress_callback=progress_callback)

    # Вшиваем все доступные субтитры (мягкими дорожками)
    subs = []
    try:
        sub_dict = stream.subtitles.subtitles or {}
    except Exception:
        sub_dict = {}
    for code, info in sub_dict.items():
        link = info.get("link")
        # Код языка приходит со страницы сайта и идёт прямо в ИМЯ ФАЙЛА. У настоящего
        # кода бывают только буквы и дефис («ru», «en», «pt-br»); что-то другое —
        # либо смена вёрстки, либо попытка увести запись в чужую папку.
        if link and _SUB_CODE_RE.match(str(code)):
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

    # Субтитров нет или вшить не удалось — переносим moov-атом в начало (faststart),
    # чтобы на iOS видео не показывалось чёрным экраном при стриминге. Без
    # перекодирования (-c copy), поэтому быстро. Если ffmpeg не справился — отдаём как есть.
    for _, sp in subs:
        _safe_rm(sp)
    if _faststart(raw, final):
        _safe_rm(raw)
        return final
    os.replace(raw, final)
    return final


def _faststart(src: str, dst: str) -> bool:
    """Ремукс mp4 с moov-атомом в начале (-c copy, без перекодирования)."""
    res = ffmpeg.run(["ffmpeg", "-y", "-i", src, "-c", "copy",
                      "-movflags", "+faststart", dst])
    return res.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0
