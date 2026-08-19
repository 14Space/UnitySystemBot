"""
Ежесуточная проверка работоспособности всего функционала бота.

Каждая проверка — «дымовой тест» реального пути: вызывает те же функции, что и живой
бот, на публичной тестовой ссылке, и смотрит, что на выходе получился непустой файл
(или корректные метаданные). Результат уходит админу вместе с дневным отчётом.

Три исхода:
  ✅ работает   — функция отработала;
  ❌ не работает — упала (в отчёте видно короткую причину);
  ⚪ пропущено   — для проверки не задана тестовая ссылка (переменная HC_URL_* в .env).

Тестовые ссылки не «зашиты намертво»: любую можно переопределить в .env (HC_URL_<КЛЮЧ>),
не трогая код — ссылки на чужих площадках со временем удаляют. Для «тяжёлых» пунктов
(фильм HDRezka, альбом целиком) намеренно проверяем только метаданные, а не полное
скачивание. Ни одна проверка не должна ронять отчёт — любое исключение перехватывается.
"""
import asyncio
import logging
import os
import time

logger = logging.getLogger(__name__)

# Потолок времени на одну проверку (сек) — чтобы зависшая площадка не тормозила отчёт.
_PROBE_TIMEOUT = int(os.getenv("HEALTHCHECK_TIMEOUT", "120"))
# Сколько проверок гоняем одновременно (чтобы не перегружать сеть и не ловить лимиты).
_CONCURRENCY = int(os.getenv("HEALTHCHECK_CONCURRENCY", "4"))


def _url(key: str, default: str = "") -> str:
    """Тестовая ссылка для проверки: из .env (HC_URL_<KEY>) или значение по умолчанию."""
    return os.getenv(f"HC_URL_{key}", default).strip()


# --- Тестовые ссылки. Публичные и по возможности «вечные». Переопределяются в .env. ---
# Пустая строка = проверка пропускается (⚪). Взрослую площадку намеренно оставили
# пустой: впиши свою ссылку в .env (HC_URL_PORNHUB / HC_URL_PORNHUB_SHORT).
U_YT_VIDEO   = _url("YT_VIDEO", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
U_YT_SHORTS  = _url("YT_SHORTS", "https://www.youtube.com/shorts/tPEE9ZwTmy0")
U_YT_MUSIC   = _url("YT_MUSIC", "https://music.youtube.com/watch?v=dQw4w9WgXcQ")
U_SPOTIFY    = _url("SPOTIFY", "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
# Редакционные плейлисты Spotify (37i9…) через публичный API не читаются — берём альбом.
U_SPOTIFY_COL = _url("SPOTIFY_COL", "https://open.spotify.com/album/4m2880jivSbbyEGAKfITCa")
U_SOUNDCLOUD = _url("SOUNDCLOUD", "https://soundcloud.com/edsheeran/shape-of-you")
U_SOUNDCLOUD_SET = _url("SOUNDCLOUD_SET", "https://soundcloud.com/edsheeran/sets/drunk-remixes")
U_IG_REEL    = _url("IG_REEL", "https://www.instagram.com/reel/DbW4UlRF4mc/")
U_IG_PHOTO   = _url("IG_PHOTO", "https://www.instagram.com/p/DcGauVjJtS8/")
U_IG_CAROUSEL = _url("IG_CAROUSEL", "https://www.instagram.com/p/DYAxmeBCLVz/")
U_TIKTOK     = _url("TIKTOK", "https://www.tiktok.com/@tiktok/video/7106594312292453675")
U_TIKTOK_SLIDE = _url("TIKTOK_SLIDE", "https://vt.tiktok.com/ZSVAe23Sh/")
U_PINTEREST_IMG = _url("PINTEREST_IMG", "https://www.pinterest.com/pin/99360735500167749/")
U_PINTEREST_VID = _url("PINTEREST_VID", "https://pin.it/2BrznYneL")
U_TWITTER_VIDEO = _url("TWITTER_VIDEO", "https://x.com/GaiaSerenity/status/2089940424815247484")
U_TWITTER_PHOTO = _url("TWITTER_PHOTO", "https://x.com/TheEllenShow/status/440322224407314432")
U_TWITTER_TEXT = _url("TWITTER_TEXT", "https://x.com/jack/status/20")
# PornHub сейчас отдаёт 403 (анти-бот площадки) — ссылки рабочие, проверка это покажет.
U_PORNHUB    = _url("PORNHUB", "https://www.pornhub.com/view_video.php?viewkey=6a757d85f1e87")
U_PORNHUB_SHORT = _url("PORNHUB_SHORT", "https://www.pornhub.com/shorties/6a16e8fcbb7ec")
U_HDREZKA    = _url("HDREZKA", "https://rezka.ag/films/fiction/45140-tron-sleduyuschiy-den-2011-latest.html")


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def _size_of(paths) -> int:
    """Суммарный размер файлов (принимает путь или список путей). Пропущенные не считаем."""
    if isinstance(paths, str):
        paths = [paths]
    total = 0
    for p in paths or []:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def _cleanup(paths):
    """Удаляет за собой скачанные тестовые файлы."""
    if isinstance(paths, str):
        paths = [paths]
    for p in paths or []:
        try:
            os.remove(p)
        except OSError:
            pass


def _kb(nbytes: int) -> str:
    return f"{nbytes // 1024} КБ" if nbytes < 1024 * 1024 else f"{nbytes / 1024 / 1024:.1f} МБ"


# ---------------------------------------------------------------------------
# Проверки функций (без скачивания)
# ---------------------------------------------------------------------------
async def _check_ai():
    from bot.features.ai.client import ask
    _text, status = await asyncio.to_thread(
        ask, [{"role": "user", "content": "Reply with the single word: pong"}], "en")
    reasons = {"quota": "исчерпан дневной лимит", "no_provider": "не задан ни один ключ",
               "error": "сбой провайдера"}
    return status == "ok", "ответ получен" if status == "ok" else reasons.get(status, status)


async def _check_currency():
    from bot.features.currency.rates import convert
    res = await convert(1.0, "USD", ["EUR"])
    if not res or "EUR" not in res:
        return False, "курсы недоступны"
    return True, f"1 USD = {res['EUR']:.2f} EUR"


async def _check_whisper():
    from bot.config import WHISPER_DEVICE

    def work():
        import bot.features.transcribe.transcriber.whisper_transcriber as w
        import faster_whisper  # noqa: F401 — важен сам факт импорта зависимости
        loaded = getattr(w, "_model", None) is not None
        return True, f"{WHISPER_DEVICE}, {'прогрета' if loaded else 'готова (не прогрета)'}"

    return await asyncio.to_thread(work)


# ---------------------------------------------------------------------------
# Проверки скачивания (реальными функциями бота)
# ---------------------------------------------------------------------------
async def _dl_quality_video(url):
    """YouTube-видео и PornHub: путь «выбор качества» — берём самое лёгкое качество."""
    from bot.features.download.downloaders.ytdlp_wrapper import (
        get_video_info, get_available_qualities, download_video)

    def work():
        info = get_video_info(url)
        quals = get_available_qualities(info)
        path = download_video(url, min(quals) if quals else 144)
        try:
            size = _size_of(path)
            return size > 0, _kb(size) if size else "файл пуст"
        finally:
            _cleanup(path)

    return await asyncio.to_thread(work)


async def _dl_shorts(url):
    from bot.features.download.downloaders.ytdlp_wrapper import download_shorts

    def work():
        path = download_shorts(url)
        try:
            size = _size_of(path)
            return size > 0, _kb(size) if size else "файл пуст"
        finally:
            _cleanup(path)

    return await asyncio.to_thread(work)


async def _dl_pornhub_short(url):
    """PornHub shorties — это обычное видео с другим URL. Переписываем в стандартный
    (как это делает боевой обработчик) и качаем лучшим качеством, как Shorts."""
    from urllib.parse import urlparse
    from bot.features.download.downloaders.ytdlp_wrapper import download_shorts

    def work():
        vid = urlparse(url).path.rstrip("/").split("/")[-1]
        std = f"https://www.pornhub.com/view_video.php?viewkey={vid}"
        path = download_shorts(std)
        try:
            size = _size_of(path)
            return size > 0, _kb(size) if size else "файл пуст"
        finally:
            _cleanup(path)

    return await asyncio.to_thread(work)


async def _dl_audio(url):
    """YT Music и SoundCloud-трек: скачиваем аудио."""
    from bot.features.download.downloaders.ytdlp_wrapper import download_audio

    def work():
        path = download_audio(url, embed_thumbnail=False)
        try:
            size = _size_of(path)
            return size > 0, _kb(size) if size else "файл пуст"
        finally:
            _cleanup(path)

    return await asyncio.to_thread(work)


async def _dl_media(url):
    """TikTok/Pinterest: универсальное скачивание одного медиа (видео или фото)."""
    from bot.features.download.downloaders.ytdlp_wrapper import download_media

    def work():
        path = download_media(url)
        try:
            size = _size_of(path)
            kind = "фото" if str(path).lower().endswith((".jpg", ".jpeg", ".png", ".webp")) else "видео"
            return size > 0, f"{kind}, {_kb(size)}" if size else "файл пуст"
        finally:
            _cleanup(path)

    return await asyncio.to_thread(work)


async def _check_spotify():
    """Трек Spotify целиком: метаданные (ключи API) → поиск источника → скачивание."""
    from bot.features.download.downloaders.spotify import get_track_info
    from bot.features.download.downloaders.music_search import find_track_source
    from bot.features.download.downloaders.ytdlp_wrapper import download_audio

    def work():
        info = get_track_info(U_SPOTIFY)
        if not info.get("title"):
            return False, "нет метаданных (проверь ключи Spotify)"
        label = f"{info['artist']} – {info['title']}"
        src = find_track_source(info["artist"].split(",")[0], info["title"], info["duration"])
        if not src:
            return False, f"источник не найден ({label})"
        path = download_audio(src, embed_thumbnail=False)
        try:
            size = _size_of(path)
            return size > 0, f"{label} — {_kb(size)}" if size else f"скачивание пусто ({label})"
        finally:
            _cleanup(path)

    return await asyncio.to_thread(work)


async def _check_spotify_collection():
    """Альбом/плейлист Spotify: проверяем только чтение списка треков (не качаем целиком)."""
    from bot.features.download.downloaders.spotify import get_collection_info

    def work():
        col = get_collection_info(U_SPOTIFY_COL)
        n = len(col.get("tracks") or [])
        return n > 0, f"{col.get('kind', '?')} «{col.get('title', '?')}», {n} треков"

    return await asyncio.to_thread(work)


async def _check_soundcloud_set():
    from bot.features.download.downloaders.ytdlp_wrapper import get_soundcloud_set

    def work():
        s = get_soundcloud_set(U_SOUNDCLOUD_SET)
        n = len(s.get("tracks") or [])
        return n > 0, f"«{s.get('title', '?')}», {n} треков"

    return await asyncio.to_thread(work)


async def _dl_reel(url):
    from bot.features.download.downloaders.instagram import download_reel

    def work():
        path = download_reel(url)
        try:
            size = _size_of(path)
            return size > 0, _kb(size) if size else "файл пуст"
        finally:
            _cleanup(path)

    return await asyncio.to_thread(work)


async def _dl_ig_post(url, expect_carousel=False):
    """Instagram-пост: фото/видео или карусель. Возвращает список файлов."""
    from bot.features.download.downloaders.instagram import download_post, is_image

    def work():
        paths = download_post(url)
        try:
            if not paths:
                return False, "пост пуст"
            size = _size_of(paths)
            photos = sum(1 for p in paths if is_image(p))
            if expect_carousel and len(paths) < 2:
                return False, f"ожидалась карусель, пришёл 1 файл ({_kb(size)})"
            return True, f"{len(paths)} файл(ов), из них фото: {photos}, {_kb(size)}"
        finally:
            _cleanup(paths)

    return await asyncio.to_thread(work)


async def _check_tiktok(url, expect_slideshow=False):
    """TikTok: дёргаем tikwm API (главная точка отказа) и проверяем тип поста."""
    from bot.features.download.downloaders import tiktok

    def work():
        post = tiktok.fetch_tiktok(url)
        kind, data = post.get("kind"), post.get("data") or {}
        if expect_slideshow:
            imgs = len(data.get("images") or [])
            return imgs > 0, f"слайдшоу, {imgs} фото" if imgs else f"ожидалось слайдшоу, тип: {kind}"
        ok = kind in ("video", "live") and bool(data)
        return ok, f"тип: {kind}"

    return await asyncio.to_thread(work)


async def _check_twitter(url, expect_text=False):
    """X (Twitter): читаем твит (главная точка отказа — API). Для медиа-твита ещё и
    скачиваем медиа. Для текстового — проверяем, что твит прочитался и в нём есть текст.
    Саму отрисовку карточки (Playwright/Chromium) тут не гоняем: этот headless-браузер
    общий с ботом, и его параллельный запуск в проверке конфликтует сам с собой."""
    from bot.features.download.downloaders import twitter

    def work():
        tweet = twitter.get_tweet(url)
        media = tweet.get("media") or []
        if expect_text:
            text = (tweet.get("text") or "").strip()
            return bool(text), f"твит прочитан: «{text[:40]}»" if text else "пустой текст твита"
        if not media:
            return False, "в твите нет медиа"
        files = twitter.download_media(media, tweet["id"])
        paths = [f["path"] for f in files]
        try:
            size = _size_of(paths)
            kinds = ", ".join(sorted({f["kind"] for f in files}))
            return size > 0, f"{kinds}, {_kb(size)}" if size else "медиа пусто"
        finally:
            _cleanup(paths)

    return await asyncio.to_thread(work)


async def _check_hdrezka():
    """HDRezka: открываем страницу и читаем инфо (озвучки/сезоны). Сам фильм не качаем."""
    from bot.features.download.downloaders import hdrezka

    def work():
        api = hdrezka.open_media(U_HDREZKA)
        info = hdrezka.get_info(api, U_HDREZKA)
        title = info.get("title") or info.get("name") or "?"
        return True, f"открыт: {title}"

    return await asyncio.to_thread(work)


# ---------------------------------------------------------------------------
# Реестр проверок. Порядок = порядок в отчёте. url="" → проверка пропускается (⚪).
# ---------------------------------------------------------------------------
_CHECKS = [
    # Скачивание
    ("YouTube — видео",        lambda: _dl_quality_video(U_YT_VIDEO),        U_YT_VIDEO),
    ("YouTube — Shorts",       lambda: _dl_shorts(U_YT_SHORTS),             U_YT_SHORTS),
    ("YouTube — YT Music",     lambda: _dl_audio(U_YT_MUSIC),               U_YT_MUSIC),
    ("Spotify — трек",         _check_spotify,                               U_SPOTIFY),
    ("Spotify — альбом/плейлист", _check_spotify_collection,                U_SPOTIFY_COL),
    ("SoundCloud — трек",      lambda: _dl_audio(U_SOUNDCLOUD),             U_SOUNDCLOUD),
    ("SoundCloud — сет",       _check_soundcloud_set,                        U_SOUNDCLOUD_SET),
    ("Instagram — Reels",      lambda: _dl_reel(U_IG_REEL),                 U_IG_REEL),
    ("Instagram — фото-пост",  lambda: _dl_ig_post(U_IG_PHOTO),             U_IG_PHOTO),
    ("Instagram — карусель",   lambda: _dl_ig_post(U_IG_CAROUSEL, True),    U_IG_CAROUSEL),
    ("TikTok — видео",         lambda: _check_tiktok(U_TIKTOK),             U_TIKTOK),
    ("TikTok — слайдшоу",      lambda: _check_tiktok(U_TIKTOK_SLIDE, True), U_TIKTOK_SLIDE),
    ("Pinterest — фото",       lambda: _dl_media(U_PINTEREST_IMG),          U_PINTEREST_IMG),
    ("Pinterest — видео",      lambda: _dl_media(U_PINTEREST_VID),          U_PINTEREST_VID),
    ("X (Twitter) — видео",    lambda: _check_twitter(U_TWITTER_VIDEO),     U_TWITTER_VIDEO),
    ("X (Twitter) — фото",     lambda: _check_twitter(U_TWITTER_PHOTO),     U_TWITTER_PHOTO),
    ("X (Twitter) — текст",    lambda: _check_twitter(U_TWITTER_TEXT, True), U_TWITTER_TEXT),
    ("PornHub — видео",        lambda: _dl_quality_video(U_PORNHUB),        U_PORNHUB),
    ("PornHub — shorties",     lambda: _dl_pornhub_short(U_PORNHUB_SHORT),  U_PORNHUB_SHORT),
    ("HDRezka — фильм/сериал", _check_hdrezka,                              U_HDREZKA),
    # Функции
    ("ИИ-ассистент",           _check_ai,                                    "x"),
    ("Конвертер валют",        _check_currency,                              "x"),
    ("Расшифровка (Whisper)",  _check_whisper,                               "x"),
]


# Проверки с Playwright (headless-браузер) гоняем строго по очереди через общий замок:
# синхронный Playwright не любит параллельный запуск. Сейчас такая одна — HDRezka
# (обход анти-бота); замок оставлен на случай появления новых.
_PLAYWRIGHT_CHECKS = {"HDRezka — фильм/сериал"}


async def _run(name: str, coro_fn, url: str, sem: asyncio.Semaphore,
               pw_lock: asyncio.Lock) -> dict:
    """Запускает одну проверку с таймаутом и перехватом ошибок. url=="" → пропуск."""
    if not url:
        return {"name": name, "state": "skip", "detail": "нет тестовой ссылки", "sec": 0.0}
    async with sem:
        start = time.monotonic()
        try:
            if name in _PLAYWRIGHT_CHECKS:
                async with pw_lock:                  # Playwright-проверки — строго по одной
                    ok, detail = await asyncio.wait_for(coro_fn(), _PROBE_TIMEOUT)
            else:
                ok, detail = await asyncio.wait_for(coro_fn(), _PROBE_TIMEOUT)
            state = "ok" if ok else "fail"
        except asyncio.TimeoutError:
            state, detail = "fail", f"таймаут > {_PROBE_TIMEOUT}с"
        except Exception as e:                       # noqa: BLE001 — отчёт важнее типа
            state, detail = "fail", f"{type(e).__name__}: {e}"[:140]
            logger.warning("Проверка «%s» упала", name, exc_info=True)
    return {"name": name, "state": state, "detail": detail,
            "sec": round(time.monotonic() - start, 1)}


async def run_health_checks() -> list[dict]:
    """Гоняет все проверки (ограничивая одновременность) и возвращает результаты по порядку."""
    sem = asyncio.Semaphore(_CONCURRENCY)
    pw_lock = asyncio.Lock()
    return list(await asyncio.gather(
        *(_run(n, fn, url, sem, pw_lock) for n, fn, url in _CHECKS)))


def format_health(results: list[dict]) -> str:
    """Блок «проверка функционала» для дневного отчёта (HTML)."""
    icons = {"ok": "✅", "fail": "❌", "skip": "⚪"}
    ok_n = sum(1 for r in results if r["state"] == "ok")
    tested = sum(1 for r in results if r["state"] != "skip")
    lines = [f"🩺 <b>Проверка функционала: {ok_n}/{tested}</b>"]
    for r in results:
        tail = f" ({r['sec']}с)" if r["state"] != "skip" else ""
        lines.append(f"{icons[r['state']]} {r['name']} — {r['detail']}{tail}")
    return "\n".join(lines)
