import http.cookiejar
import json
import logging
import os
import re
import uuid
import requests
import yt_dlp
from urllib.parse import urlparse
from bot.config import DOWNLOADS_DIR, INSTAGRAM_COOKIES, COOKIE_COPIES_DIR
from bot.features.download.downloaders.ytdlp_wrapper import (
    BASE_OPTS, _quality_opts, _unique_outtmpl,
)
from bot.utils import cookie_files, media_names, net, pw_thread
from bot.utils import files as file_utils
from bot.utils.platform_detector import normalize_cache_url


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

logger = logging.getLogger(__name__)

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXTS = (".mp4", ".mov", ".webm")

def _home() -> str:
    """Домашний туннель для Instagram («» – не настроен). Какую переменную брать,
    решает net.proxy_for – одинаково для загрузки и для проверки кук."""
    return net.proxy_for("instagram")


def _attempts(cookies: str | None) -> list[tuple[str, str | None]]:
    """Пары (прокси, куки) по порядку попыток.

    Главное правило: ЗАПРОС С КУКАМИ ИДЁТ ТОЛЬКО ЧЕРЕЗ ДОМ. Instagram смотрит, откуда
    работает сессия, и вход из дата-центра считает угоном — закрывает её. Именно это у
    нас и происходило: сессия жила ровно до ближайшего деплоя, потому что сразу после
    запуска проверка стучалась в Instagram с серверного адреса (пять обращений подряд:
    Reels, Reels со сжатием, фото-пост, карусель, сами куки). Выглядело как «куки опять
    протухли», хотя срок у них до 2027 года — их просто отзывали.

    Без кук прямой заход остаётся: публичный контент так качается быстрее (замер с
    сервера: напрямую 4.0с, через туннель 9.3с), а светить нечего.

    Порядок: сначала дом с куками (всё, что умеем), затем — прямой заход БЕЗ кук.
    Второй нужен на случай, когда туннель лежит: открытый пост скачается и так, а
    сессию мы при этом не подставим.
    """
    home = _home()
    if cookies and home:
        return [(home, cookies), ("", None)]
    if cookies:                      # туннель не настроен — работаем как раньше
        return [("", cookies)]
    return [("", None)]


def _reel_attempts(cookies: str | None) -> list[tuple[str, str | None]]:
    """Для Reels – сначала гостем, аккаунт только если гостем не вышло.

    26.09.2026 Instagram показал на аккаунте «подозреваем автоматизацию»: каждый
    запрос бота шёл от его имени. Открытый Reel гостю отдаётся (и напрямую быстрее,
    см. _attempts), так что сессию тратим лишь на закрытые. Фото и карусели гостю не
    отдаются вовсе (фото – никак, карусель – миниатюрами), им порядок не меняем."""
    return [("", None)] + [a for a in _attempts(cookies) if a[1]]


# --- Запасной путь для одиночного ФОТО через браузер (Playwright) -------------
# yt-dlp Instagram-экстрактор на посте без видео падает («There is no video in this
# post»), а HTML Instagram теперь пустой JS-каркас (ни og:image, ни display_url).
# Поэтому одиночное фото добываем рендером страницы-эмбеда настоящим браузером. Весь
# Playwright гоняем на выделенном потоке (pw_thread) — общий браузер нельзя дёргать с
# разных потоков пула asyncio.to_thread (иначе «greenlet: cannot switch to a different
# thread» или конфликт event loop). Карусели/видео сюда не доходят — берутся через yt-dlp.


def _pw_cookies() -> list[dict]:
    """Куки Instagram в формате Playwright (из того же cookies.txt, что и у yt-dlp)."""
    path = _cookies_path()
    if not path:
        return []
    cj = http.cookiejar.MozillaCookieJar(path)
    try:
        cj.load(ignore_discard=True, ignore_expires=True)
    except Exception:
        return []
    return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path or "/"}
            for c in cj]


def _embed_image_src(browser, shortcode: str) -> str | None:
    """Рендерит страницу-эмбед поста общим браузером и возвращает ссылку на картинку из
    DOM (или None). Выполняется строго на выделенном Playwright-потоке (см. pw_thread).

    Куки уходят ТОЛЬКО через дом. Правило то же, что и для обычных запросов: свою
    сессию, увиденную из дата-центра, Instagram считает угоном и закрывает вход. Здесь
    оно было нарушено — браузер получал куки, а ходил напрямую с адреса сервера, и
    23.09.2026 сессия умерла в очередной раз. Туннеля нет — идём ГОСТЕМ: пусть кадр
    не достанется, это дешевле потерянного входа.
    """
    cookies = _pw_cookies()
    home = _home()
    with_session = bool(cookies and home)
    options = {"user_agent": _UA}
    if with_session:
        options["proxy"] = {"server": home}
    ctx = browser.new_context(**options)
    try:
        if with_session:
            ctx.add_cookies(cookies)
        page = ctx.new_page()
        page.goto(f"https://www.instagram.com/p/{shortcode}/embed/captioned/",
                  wait_until="networkidle", timeout=45000)
        if not page.query_selector("img"):
            return None
        return page.eval_on_selector(
            "img.EmbeddedMediaImage, article img, img[decoding]",
            "e => e.currentSrc || e.src",
        )
    finally:
        ctx.close()


# Где в HTML поста лежит ссылка на картинку. Instagram время от времени перекладывает
# её и меняет вёрстку, поэтому ищем НЕСКОЛЬКИМИ способами и берём первый сработавший.
_IMG_PATTERNS = (
    re.compile(r'"display_url":"([^"]+)"'),
    re.compile(r'property="og:image"\s+content="([^"]+)"'),
    re.compile(r'"src":"(https://[^"]*cdninstagram[^"]*)"'),
)


def _image_from_html(html: str) -> str | None:
    """Ссылка на картинку из HTML страницы поста."""
    for pattern in _IMG_PATTERNS:
        found = pattern.search(html or "")
        if found:
            # В JSON внутри HTML символы экранированы: \u0026 вместо & и \/ вместо /.
            return found.group(1).replace("\\u0026", "&").replace("\\/", "/")
    return None


def _photo_via_page(shortcode: str, cookies: str | None, proxies=None) -> list[str]:
    """Одиночное фото: берём ссылку прямо из HTML поста, С КУКАМИ.

    yt-dlp такие посты не умеет вовсе («There is no video in this post»), а эмбед,
    на который мы раньше опирались, Instagram с некоторых пор гостю не отдаёт — вместо
    страницы приходит проверка на вход. С куками вошедшего страница открывается как
    обычно, и ссылка на картинку есть прямо в её тексте: браузер для этого не нужен.
    """
    jar = None
    if cookies and os.path.exists(cookies):
        jar = http.cookiejar.MozillaCookieJar(cookies)
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except OSError:
            jar = None

    for url in (f"https://www.instagram.com/p/{shortcode}/",
                f"https://www.instagram.com/p/{shortcode}/embed/captioned/"):
        try:
            r = requests.get(url, headers={"User-Agent": _UA}, cookies=jar,
                             proxies=proxies, timeout=30)
            src = _image_from_html(r.text)
        except Exception:
            logger.info("Instagram: %s не отдал картинку", url, exc_info=True)
            continue
        if not src:
            continue
        path = os.path.join(DOWNLOADS_DIR, f"{shortcode}_{uuid.uuid4().hex[:8]}_dl.jpg")
        # Потоком на диск: раньше картинка (а в карусели — видео) сначала целиком
        # оказывалась в памяти, см. bot/utils/net.py.
        try:
            net.fetch_to_file(src, path, proxies=proxies,
                              headers={"User-Agent": _UA}, timeout=60)
        except Exception:
            logger.info("Instagram: картинка не скачалась", exc_info=True)
            continue
        return [path]
    return []


def _photo_via_browser(shortcode: str, proxies=None) -> list[str]:
    """Тот же кадр, но рендером эмбеда в браузере. Оставлен последним шансом: тяжелее
    (поднимает Chromium) и ломается от смены вёрстки, зато иногда добирается там, где
    обычный запрос упирается в проверку."""
    src = pw_thread.run_with_browser(_embed_image_src, shortcode)
    if not src:
        return []
    path = os.path.join(DOWNLOADS_DIR, f"{shortcode}_{uuid.uuid4().hex[:8]}_dl.jpg")
    net.fetch_to_file(src, path, proxies=proxies,
                      headers={"User-Agent": _UA}, timeout=60)
    return [path]


# --- Медиа прямо со страницы поста, С КУКАМИ ---------------------------------
#
# 24.09.2026: Reel «только для вошедших» не качался, хотя куки живые (страница
# настроек аккаунта отдаётся). Разбор показал: yt-dlp с куками идёт в закрытый API
# Instagram (/api/v1/media/<id>/info/), а тот нашей сессии отвечает переходом на
# главную, как гостю, – на ЛЮБОЙ пост, не только закрытый. yt-dlp видит пустой ответ,
# бот уходил на запасной заход без кук и честно говорил «только для вошедших». То есть
# куки не работали вовсе: открытое качалось запасным заходом, закрытое – никак. А у
# каруселей гостю достаются только миниатюры: фото приходили по 6–7 КБ вместо
# 2717x3233, и проверка функционала этого не видела – «скачалось» для неё и есть
# «работает».
#
# Страница самого поста, открытая с теми же куками, всё это содержит: в её данных
# (JSON внутри <script type="application/json">) лежит объект поста с кодом, а в нём
# carousel_media, video_versions и image_versions2 с прямыми ссылками в полном
# размере. Отдаёт она их только запросу, похожему на браузер (обычный запрос получает
# страницу без данных), поэтому здесь curl_cffi с маскировкой под Chrome – он уже
# стоит ради PornHub.

_JSON_SCRIPT_RE = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)


def _find_media(obj, shortcode: str) -> dict | None:
    """Объект поста с этим кодом где-то в глубине данных страницы. На странице есть и
    соседние посты (лента, «ещё от автора»), поэтому сверяем именно код."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if cur.get("code") == shortcode and (cur.get("carousel_media")
                                                  or cur.get("video_versions")
                                                  or cur.get("image_versions2")):
                return cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _area(v: dict) -> int:
    return (v.get("width") or 0) * (v.get("height") or 0)


def _best_version(item: dict) -> tuple[str, bool] | None:
    """(ссылка, это_видео) – лучшее видео элемента, а если его нет – самое большое фото."""
    videos = [v for v in item.get("video_versions") or [] if isinstance(v, dict) and v.get("url")]
    if videos:
        return max(videos, key=_area)["url"], True
    cands = [c for c in (item.get("image_versions2") or {}).get("candidates") or []
             if isinstance(c, dict) and c.get("url")]
    if cands:
        return max(cands, key=_area)["url"], False
    return None


def _page_items(html: str, shortcode: str) -> list[tuple[str, bool]]:
    """Все элементы поста по порядку: [(ссылка, это_видео)]. Пусто – не нашли."""
    for m in _JSON_SCRIPT_RE.finditer(html or ""):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        media = _find_media(data, shortcode)
        if media:
            items = [_best_version(it) for it in (media.get("carousel_media") or [media])]
            items = [it for it in items if it]
            if items:
                return items
    return []


def _page_media(shortcode: str, cookies: str, proxy: str) -> list[tuple[str, bool]]:
    """Элементы поста со страницы, С КУКАМИ и тем же путём, что и остальные запросы с
    куками (через дом, если туннель настроен)."""
    from curl_cffi import requests as curl

    jar = http.cookiejar.MozillaCookieJar(cookies)
    jar.load(ignore_discard=True, ignore_expires=True)
    # socks5h, а не socks5: адрес Instagram тогда разрешается ДОМА. При socks5 его
    # разрешает сервер, получает IPv6, а у домашнего канала нормального IPv6 нет –
    # соединение не устанавливается вовсе.
    home = proxy.replace("socks5://", "socks5h://", 1) if proxy else ""
    for url in (f"https://www.instagram.com/p/{shortcode}/",
                f"https://www.instagram.com/reels/{shortcode}/"):
        try:
            r = curl.get(url, cookies={c.name: c.value for c in jar},
                         proxies={"http": home, "https": home} if home else None,
                         impersonate="chrome", timeout=30)
            items = _page_items(r.text, shortcode)
        except Exception:
            logger.info("Instagram: страница %s не отдала медиа", url, exc_info=True)
            continue
        if items:
            return items
    return []


def _download_items(items: list[tuple[str, bool]], shortcode: str, proxy: str) -> list[str]:
    """Качает элементы поста по порядку. Упал любой – убираем уже скачанное и бросаем:
    полкарусели хуже, чем честный запасной путь."""
    paths: list[str] = []
    try:
        for i, (src, is_vid) in enumerate(items, 1):
            ext = ".mp4" if is_vid else ".jpg"
            path = os.path.join(DOWNLOADS_DIR, f"{shortcode}_{i}_{uuid.uuid4().hex[:8]}_dl{ext}")
            net.fetch_to_file(src, path, proxies=net.as_requests(proxy),
                              headers={"User-Agent": _UA}, timeout=120)
            path = _fix_type(path)
            if path:
                paths.append(path)
    except Exception:
        file_utils.remove(paths)
        raise
    return paths


def _fix_type(path: str) -> str | None:
    """Фото это или видео, решает дальше расширение (is_image), а его мы взяли из
    данных поста. Сверяем с содержимым: не то, за что себя выдаёт, – переименовываем,
    а не пойми что (страница-заглушка) не отправляем вовсе."""
    kind = file_utils.sniff(path)
    if kind is None:
        logger.warning("Instagram: по ссылке пришло не фото и не видео — пропускаю")
        file_utils.remove(path)
        return None
    if (kind[0] == "image") != is_image(path):
        real = os.path.splitext(path)[0] + kind[1]
        os.replace(path, real)
        return real
    return path


def _cookies_path() -> str | None:
    """Путь к ОДНОРАЗОВОЙ КОПИИ файла кук, если он задан и существует.

    Копия, а не оригинал: yt-dlp пишет файл кук обратно и затирает ключ входа тем, что
    прислал Instagram. Подробности — в bot/utils/cookie_files.py.
    """
    # Копии кладём в СВОЮ папку, не в downloads: тот том виден контейнеру Bot API,
    # и держать там копии живой сессии незачем (а уборка хвостов их ещё и сносила).
    return cookie_files.disposable(INSTAGRAM_COOKIES, COOKIE_COPIES_DIR)


# Настоящий код поста — это буквы, цифры, дефис и подчёркивание. Ничего другого в
# нём не бывает, а вот попасть туда могло: код идёт прямо в ИМЯ ФАЙЛА, и косая черта
# или «..» в нём означали бы запись не туда, куда мы думаем.
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _shortcode(url: str) -> str | None:
    """Достаёт код поста из ссылки: /p/CODE/, /reel/CODE/, /tv/CODE/"""
    m = re.search(r"/(?:p|reel|reels|tv)/([^/?#]+)", urlparse(url).path)
    if not m:
        return None
    code = m.group(1)
    if not _SHORTCODE_RE.match(code):
        logger.info("Instagram: подозрительный код поста %r — не берусь", code[:40])
        return None
    return code


def _canonical(url: str) -> str:
    """Ссылка вида www.instagram.com/reel/КОД. Зеркала (ddinstagram) и хвосты отбрасываем:
    извлекатель yt-dlp понимает только сам instagram.com, а «generic», который раньше
    подбирал зеркала, выключен (см. ALLOWED_EXTRACTORS в ytdlp_wrapper)."""
    return normalize_cache_url(url)


def download_reel(url: str, max_height: int | None = None) -> str:
    """Скачивает Instagram Reel (видео) — как Shorts. max_height ограничивает качество
    («Сжатие шортс»); без него берём максимум."""
    url = _canonical(url)
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    output_path = _unique_outtmpl()

    base_opts = {
        **BASE_OPTS,
        **_quality_opts(max_height),
        "outtmpl": output_path,
        "merge_output_format": "mp4",
    }
    # Куки залогиненного аккаунта — чтобы качать Reels с пометкой «доступ не для всех»
    cookies = _cookies_path()
    if cookies:
        base_opts["cookiefile"] = cookies

    last_err: Exception | None = None
    attempts = _reel_attempts(cookies)
    for n, (proxy, use_cookies) in enumerate(attempts, 1):
        ydl_opts = dict(base_opts)
        if proxy:
            ydl_opts["proxy"] = proxy
        if use_cookies:
            ydl_opts["cookiefile"] = use_cookies
        else:
            ydl_opts.pop("cookiefile", None)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                extracted = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(extracted)
                if not os.path.exists(filename):
                    filename = filename.rsplit(".", 1)[0] + ".mp4"
                media_names.remember(filename, extracted.get("title"), extracted.get("id"))
                return filename
        except Exception as e:
            last_err = e
            # С куками yt-dlp упирается в закрытый API, который нашей сессии не отвечает
            # (см. «Медиа прямо со страницы поста»), – берём видео со страницы.
            if use_cookies:
                code = _shortcode(url)
                try:
                    videos = [it for it in (_page_media(code, use_cookies, proxy) if code else [])
                              if it[1]]
                    paths = _download_items(videos[:1], code, proxy) if videos else []
                except Exception:
                    logger.info("Instagram: со страницы Reel не вышло", exc_info=True)
                    paths = []
                if paths:
                    media_names.remember(paths[0], None, code)
                    logger.info("Instagram: Reel %s взят со страницы (с куками)", code)
                    return paths[0]
            # Есть ещё способ (гостем не отдали – пробуем аккаунтом через дом).
            if n < len(attempts):
                logger.info("Instagram: Reel %s не вышло (%s) — пробую следующий способ",
                            "гостем" if not use_cookies else "с куками", str(e)[:60])
                continue
            # «Доступ не для всех» — почти всегда вопрос кук. Подсказываем в лог, что делать.
            if "audiences" in str(e).lower() or "available to everyone" in str(e).lower():
                if not cookies:
                    logger.warning("Instagram: контент только для вошедших, а cookies.txt нет — "
                                   "добавь файл кук (INSTAGRAM_COOKIES), см. data/instagram_cookies.txt")
                else:
                    logger.warning("Instagram: контент только для вошедших, и даже с куками "
                                   "не вышло — проверь пункт «Куки Instagram» (/test); "
                                   "если он зелёный, сессия жива, а сломался способ добычи")
            raise
    raise last_err


class PostUnavailable(Exception):
    """Пост не отдаётся: приватный, удалён или недоступен этому аккаунту."""


def _best_media(entry: dict) -> tuple[str, bool] | None:
    """Из элемента поста достаёт (ссылка_на_медиа, это_видео).
    Видео — лучший из formats; фото — самый крупный thumbnail. None, если пусто."""
    formats = entry.get("formats") or []
    if formats:
        # Берём формат с наибольшим разрешением (у Instagram они уже с прямыми ссылками)
        best = max(formats, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        if best.get("url"):
            return best["url"], True
    thumbs = entry.get("thumbnails") or []
    if thumbs:
        best = max(thumbs, key=lambda t: (t.get("height") or 0, t.get("width") or 0))
        if best.get("url"):
            return best["url"], False
    return None


def download_post(url: str) -> list[str]:
    """
    Скачивает пост Instagram целиком: одно фото/видео или всю карусель.
    Через yt-dlp: instaloader ходил в graphql, который Instagram закрыл (403/400),
    и посты перестали качаться совсем. Возвращает список файлов по порядку.
    """
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    url = _canonical(url)
    shortcode = _shortcode(url) or "ig"

    base_opts = {
        **BASE_OPTS,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        # Фото-элементы не имеют видео-форматов: без этого yt-dlp роняет весь пост
        "ignoreerrors": True,
    }
    cookies = _cookies_path()
    if cookies:
        base_opts["cookiefile"] = cookies

    # Первым делом – страница поста с куками: там полные размеры и закрытые посты,
    # которых yt-dlp с нашей сессией не получает вовсе (см. «Медиа прямо со страницы»).
    for proxy, use_cookies in _attempts(cookies):
        if not use_cookies:
            continue
        try:
            items = _page_media(shortcode, use_cookies, proxy)
            files = _download_items(items, shortcode, proxy) if items else []
        except Exception:
            logger.info("Instagram: со страницы поста не вышло", exc_info=True)
            files = []
        if files:
            logger.info("Instagram: пост %s взят со страницы (с куками), файлов: %d",
                        shortcode, len(files))
            return files

    # С куками — через дом, запасной заход — напрямую без кук (см. _attempts).
    info, used_proxy, last_err = None, "", None
    for proxy, use_cookies in _attempts(cookies):
        opts = dict(base_opts)
        if proxy:
            opts["proxy"] = proxy
        if use_cookies:
            opts["cookiefile"] = use_cookies
        else:
            opts.pop("cookiefile", None)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                # process=False — не даём yt-dlp падать на фото («No video formats found»);
                # ссылки на медиа уже есть в самих элементах.
                info = ydl.extract_info(url, download=False, process=False)
        except Exception as e:
            last_err, info = e, None
        if info:
            used_proxy = proxy
            break
        if proxy:
            logger.info("Instagram: пост не отдался через дом — пробую напрямую без кук")

    if not info:
        # Частый случай — одиночное ФОТО: yt-dlp падает («There is no video in this post»).
        # Сначала пробуем просто прочитать страницу поста с куками, и лишь потом —
        # браузер: он тяжелее и ломается от каждой смены вёрстки.
        ig_proxies = net.as_requests(_home())
        for attempt, grab in (("страницей", lambda: _photo_via_page(shortcode, cookies, ig_proxies)),
                              ("браузером", lambda: _photo_via_browser(shortcode, ig_proxies))):
            try:
                photo = grab()
                if photo:
                    logger.info("Instagram: фото добыто %s", attempt)
                    return photo
            except Exception:
                logger.info("Instagram: добыть фото %s не вышло", attempt, exc_info=True)
        raise PostUnavailable(f"Instagram не отдал пост {shortcode}"
                              + (f": {last_err}" if last_err else ""))

    # Медиа-файлы качаем тем же путём (прямо или через прокси), что и метаданные.
    proxies = net.as_requests(used_proxy)

    # Карусель приходит плейлистом, одиночный пост — обычным элементом
    entries = list(info.get("entries") or [info])

    files = []
    for i, entry in enumerate(entries, 1):
        if not entry:
            continue
        media = _best_media(entry)
        if not media:
            continue
        media_url, is_vid = media
        ext = ".mp4" if is_vid else ".jpg"
        # Уникальный суффикс на каждый файл: два запроса одной карусели иначе пишут одни
        # и те же имена, и очистка одного удаляет файлы другого во время отправки альбома.
        path = os.path.join(DOWNLOADS_DIR, f"{shortcode}_{i}_{uuid.uuid4().hex[:8]}_dl{ext}")
        # Потоком на диск, с пределом размера: карусель бывает из видео, и держать
        # их в памяти целиком незачем (см. bot/utils/net.py).
        net.fetch_to_file(media_url, path, proxies=proxies, timeout=60)
        path = _fix_type(path)
        if path:
            files.append(path)

    return files


def is_image(path: str) -> bool:
    return path.lower().endswith(IMAGE_EXTS)


def is_video(path: str) -> bool:
    return path.lower().endswith(VIDEO_EXTS)
