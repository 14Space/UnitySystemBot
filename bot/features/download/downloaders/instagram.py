import http.cookiejar
import logging
import os
import re
import uuid
import requests
import yt_dlp
from urllib.parse import urlparse
from bot.features.download.downloaders.ytdlp_wrapper import (
    BASE_OPTS, DOWNLOADS_DIR, _quality_opts, _unique_outtmpl,
)
from bot.utils import pw_thread
from bot.utils import media_names
from bot.utils import cookie_files


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

try:
    from bot.config import INSTAGRAM_COOKIES, INSTAGRAM_PROXY
except Exception:  # worker может запускаться отдельно от бота
    INSTAGRAM_COOKIES = os.getenv("INSTAGRAM_COOKIES", "data/instagram_cookies.txt")
    INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "")

logger = logging.getLogger(__name__)

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXTS = (".mp4", ".mov", ".webm")

# Признаки, что Instagram отказал по анти-боту (а не «пост удалён»): при них есть смысл
# повторить запрос через прокси с другого IP.
# Признаки, по которым считаем, что Instagram нас отшил и стоит повторить через прокси.
# «failed to parse json» и «expecting value» — это ПУСТОЙ ответ: Instagram отдаёт тело
# нулевой длины, а падает уже разбор JSON внутри yt-dlp. Ровно тот же почерк, что у
# tikwm на TikTok: формально запрос успешен, отказа не видно. Без этих двух строк
# запасной путь через прокси не включался вовсе — ошибка не опознавалась как блокировка,
# и проверка Instagram краснела, хотя через домашний адрес всё скачивалось.
_BLOCK_MARKERS = ("403", "429", "400", "rate limit", "checkpoint",
                  "login required", "empty media response", "temporarily",
                  "failed to parse json", "expecting value")


def _looks_blocked(err: Exception) -> bool:
    s = str(err).lower()
    return any(m in s for m in _BLOCK_MARKERS)


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
    if cookies and INSTAGRAM_PROXY:
        return [(INSTAGRAM_PROXY, cookies), ("", None)]
    if cookies:                      # туннель не настроен — работаем как раньше
        return [("", cookies)]
    return [("", None)]


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
    DOM (или None). Выполняется строго на выделенном Playwright-потоке (см. pw_thread)."""
    ctx = browser.new_context(user_agent=_UA)
    try:
        cookies = _pw_cookies()
        if cookies:
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


def _photo_via_browser(shortcode: str, proxies=None) -> list[str]:
    """Одиночное фото Instagram: добываем картинку рендером эмбеда браузером и качаем."""
    src = pw_thread.run_with_browser(_embed_image_src, shortcode)
    if not src:
        return []
    path = os.path.join(DOWNLOADS_DIR, f"{shortcode}_{uuid.uuid4().hex[:8]}_dl.jpg")
    data = requests.get(src, timeout=60, proxies=proxies, headers={"User-Agent": _UA}).content
    with open(path, "wb") as f:
        f.write(data)
    return [path]


def _cookies_path() -> str | None:
    """Путь к ОДНОРАЗОВОЙ КОПИИ файла кук, если он задан и существует.

    Копия, а не оригинал: yt-dlp пишет файл кук обратно и затирает ключ входа тем, что
    прислал Instagram. Подробности — в bot/utils/cookie_files.py.
    """
    return cookie_files.disposable(INSTAGRAM_COOKIES, DOWNLOADS_DIR)


def _shortcode(url: str) -> str | None:
    """Достаёт код поста из ссылки: /p/CODE/, /reel/CODE/, /tv/CODE/"""
    m = re.search(r"/(?:p|reel|reels|tv)/([^/?#]+)", urlparse(url).path)
    return m.group(1) if m else None


def download_reel(url: str, max_height: int | None = None) -> str:
    """Скачивает Instagram Reel (видео) — как Shorts. max_height ограничивает качество
    («Сжатие шортс»); без него берём максимум."""
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
    for proxy, use_cookies in _attempts(cookies):
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
            # Прямой доступ заблокирован анти-ботом, а прокси ещё не пробовали — повторим.
            if proxy and INSTAGRAM_PROXY:
                logger.info("Instagram: через дом не вышло (%s) — пробую напрямую без кук",
                            str(e)[:60])
                continue
            # «Доступ не для всех» — почти всегда вопрос кук. Подсказываем в лог, что делать.
            if "audiences" in str(e).lower() or "available to everyone" in str(e).lower():
                if not cookies:
                    logger.warning("Instagram: контент только для вошедших, а cookies.txt нет — "
                                   "добавь файл кук (INSTAGRAM_COOKIES), см. data/instagram_cookies.txt")
                else:
                    logger.warning("Instagram: контент только для вошедших, но даже с куками отказ — "
                                   "скорее всего сессия протухла, перевыгрузи cookies.txt")
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
        if proxy and INSTAGRAM_PROXY:
            logger.info("Instagram: пост не отдался через дом — пробую напрямую без кук")

    if not info:
        # Частый случай — одиночное ФОТО: yt-dlp падает («There is no video in this post»).
        # Пробуем добыть картинку рендером эмбеда браузером (карусели/видео сюда не доходят).
        try:
            photo = _photo_via_browser(shortcode)
            if photo:
                return photo
        except Exception:
            logger.info("Instagram: браузерный фолбэк для фото не сработал", exc_info=True)
        raise PostUnavailable(f"Instagram не отдал пост {shortcode}"
                              + (f": {last_err}" if last_err else ""))

    # Медиа-файлы качаем тем же путём (прямо или через прокси), что и метаданные.
    proxies = {"http": used_proxy, "https": used_proxy} if used_proxy else None

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
        data = requests.get(media_url, timeout=60, proxies=proxies).content
        with open(path, "wb") as f:
            f.write(data)
        files.append(path)

    return files


def is_image(path: str) -> bool:
    return path.lower().endswith(IMAGE_EXTS)


def is_video(path: str) -> bool:
    return path.lower().endswith(VIDEO_EXTS)
