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
import re
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.config import ADMIN_LANG, STT_ORDER
from bot.utils.i18n import t, t_check

logger = logging.getLogger(__name__)

# Часовой пояс админа — для отметки времени последней проверки в /statistics.
_ADMIN_ZONE = ZoneInfo(os.getenv("ADMIN_TZ", "Europe/Chisinau"))

# Потолок времени на одну проверку (сек) — чтобы зависшая площадка не тормозила отчёт.
_PROBE_TIMEOUT = int(os.getenv("HEALTHCHECK_TIMEOUT", "120"))
# Сколько проверок гоняем одновременно (чтобы не перегружать сеть и не ловить лимиты).
_CONCURRENCY = int(os.getenv("HEALTHCHECK_CONCURRENCY", "4"))
# Пауза перед ПОВТОРНОЙ проверкой упавшего пункта. Внешние сервисы иногда икают (разовая
# 500/таймаут/анти-бот) — чтобы не слать ложную тревогу, упавший пункт перепроверяем один
# раз через эту паузу и считаем сбоем, только если он упал дважды подряд.
_RETRY_DELAY = int(os.getenv("HEALTHCHECK_RETRY_DELAY", "8"))


def _url(key: str, default: str = "") -> str:
    """Тестовая ссылка для проверки: из .env (HC_URL_<KEY>) или значение по умолчанию."""
    return os.getenv(f"HC_URL_{key}", default).strip()


# --- Тестовые ссылки. Публичные и по возможности «вечные». Переопределяются в .env. ---
# Пустая строка = проверка пропускается (⚪). Взрослую площадку намеренно оставили
# пустой: впиши свою ссылку в .env (HC_URL_PORNHUB / HC_URL_PORNHUB_SHORT).
U_YT_VIDEO   = _url("YT_VIDEO", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
U_YT_SHORTS  = _url("YT_SHORTS", "https://www.youtube.com/shorts/tPEE9ZwTmy0")
# Для проверки СЖАТИЯ нужен ролик заведомо выше потолка, иначе проверять нечего:
# ролик выше (1 секунда, максимум 480p) под потолок 720 просто не попадает, и пункт
# зеленел бы впустую — то есть ту поломку, ради которой он заведён, не поймал бы.
U_YT_SHORTS_HQ = _url("YT_SHORTS_HQ", "https://www.youtube.com/shorts/I6iy-0bnles")
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
U_TWITTER_GIF = _url("TWITTER_GIF", "")
# PornHub сейчас отдаёт 403 (анти-бот площадки) — ссылки рабочие, проверка это покажет.
U_PORNHUB    = _url("PORNHUB", "https://www.pornhub.com/view_video.php?viewkey=6a757d85f1e87")
U_PORNHUB_SHORT = _url("PORNHUB_SHORT", "https://www.pornhub.com/shorties/6a16e8fcbb7ec")
# Ролик с возрастным ограничением: на нём проверяются куки YouTube. Без входа yt-dlp
# отвечает «Sign in to confirm your age» — проверено, это и есть признак мёртвых кук.
U_YT_AGE     = _url("YT_AGE", "https://www.youtube.com/watch?v=qkO6iBwcoe4")
U_HDREZKA    = _url("HDREZKA", "https://rezka.ag/films/fiction/45140-tron-sleduyuschiy-den-2011-latest.html")
U_HDREZKA_SERIES = _url("HDREZKA_SERIES", "https://rezka.ag/cartoons/comedy/13469-multachki-bayki-metra-2008.html#t:56-s:1-e:1")


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
    return f"{nbytes // 1024}КБ" if nbytes < 1024 * 1024 else f"{nbytes / 1024 / 1024:.1f}МБ"


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


_YTDLP_NIGHTLY_API = ("https://api.github.com/repos/yt-dlp/"
                      "yt-dlp-nightly-builds/releases/latest")


def _ver_parts(v: str) -> tuple:
    """Числа версии по порядку: «2026.8.30.232658.dev0» и «2026.08.30.232658» дают
    одно и то же. Строкой их сравнивать нельзя – мешают ведущий ноль и хвост .dev."""
    return tuple(int(x) for x in re.findall(r"\d+", (v or "").split(".dev")[0]))


async def _check_ytdlp():
    """Свежесть yt-dlp: сайты ломают качалку часто, и починка приходит в ночную сборку.
    Отстали – значит часть площадок может отвалиться в любой момент."""
    from importlib import metadata as meta
    try:
        installed = meta.version("yt-dlp")
    except Exception:
        return False, t("hc_ytdlp_none", _admin_lang())

    def _latest() -> str:
        return requests.get(_YTDLP_NIGHTLY_API, timeout=20).json().get("tag_name") or ""

    try:
        latest = await asyncio.to_thread(_latest)
    except Exception as e:
        # Сеть/лимит GitHub – это не поломка бота, версию просто не с чем сравнить.
        return True, t("hc_no_compare", _admin_lang(), have=f"{installed} [{type(e).__name__}]")
    if not latest:
        return True, t("hc_no_compare", _admin_lang(), have=installed)
    if _ver_parts(installed) >= _ver_parts(latest):
        return True, installed
    return False, t("hc_ytdlp_old", _admin_lang(), have=installed, latest=latest)


# Браузерный User-Agent: с «python-requests» площадки отвечают иначе и проверка соврёт.
_COOKIE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
# Открытый ключ веб-клиента X: с ним ходит сам сайт, и без него его API не отвечает.
_X_BEARER = ("AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
             "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")


def _load_cookies(path: str):
    """Читает файл кук (формат Netscape) или возвращает None, если его нет."""
    import http.cookiejar
    if not path or not os.path.exists(path):
        return None
    jar = http.cookiejar.MozillaCookieJar(path)
    # ignore_expires: срок проверяем сами и говорим про него понятным языком.
    jar.load(ignore_discard=True, ignore_expires=True)
    return jar


def _cookie_expiry_problem(jar, name: str) -> str | None:
    """Проблема со сроком куки входа: её нет или она уже просрочена."""
    cookie = next((c for c in jar if c.name == name), None)
    if cookie is None:
        return t("hc_ck_nokey", _admin_lang(), key=name)
    if cookie.expires and cookie.expires < time.time():
        return t("hc_ck_expired", _admin_lang(), key=name)
    return None


async def _check_ig_cookies():
    """Живы ли куки Instagram.

    Зачем отдельный пункт: обычные Reels качаются и БЕЗ входа, поэтому остальные
    проверки остаются зелёными, а закрытый контент уже не скачивается. Протухание
    видно только здесь – и лучше узнать о нём до того, как заметят пользователи.
    Страница настроек аккаунта вошедшему отдаёт 200, а гостя уводит на вход (302).
    """
    from bot.features.download.downloaders.instagram import INSTAGRAM_COOKIES

    jar = _load_cookies(INSTAGRAM_COOKIES)
    if jar is None:
        return False, t("hc_ck_missing", _admin_lang(), path=INSTAGRAM_COOKIES)
    problem = _cookie_expiry_problem(jar, "sessionid")
    if problem:
        return False, problem

    def work():
        r = requests.get("https://www.instagram.com/accounts/edit/", cookies=jar,
                         headers={"User-Agent": _COOKIE_UA}, timeout=25,
                         allow_redirects=False)
        if r.status_code == 200:
            return True, t("hc_ck_alive", _admin_lang())
        if r.status_code in (301, 302):
            return False, t("hc_ck_dead", _admin_lang())
        return True, t("hc_ck_unclear", _admin_lang(), code=r.status_code)

    return await asyncio.to_thread(work)


async def _check_yt_cookies():
    """Живы ли куки YouTube.

    Нужны ровно для одного: роликов с возрастным ограничением. Без входа yt-dlp
    отвечает «Sign in to confirm your age», и ролик не скачивается совсем. Обычные
    видео идут и без кук — поэтому все прочие пункты YouTube останутся зелёными, а
    возрастные тихо перестанут работать. Заметить это можно только здесь.

    Проверяем не запросом к сайту, а попыткой прочитать ВОЗРАСТНОЙ ролик: именно она
    и отвечает на вопрос «работает ли то, ради чего куки нужны». Скачивания нет,
    только чтение данных — трафик не тратим.
    """
    from bot.config import YOUTUBE_COOKIES
    from bot.features.download.downloaders.ytdlp_wrapper import (
        BASE_OPTS, _cookie_opts, _with_music_fallback)

    if not _load_cookies(YOUTUBE_COOKIES):
        return False, t("hc_ck_missing", _admin_lang(), path=YOUTUBE_COOKIES)
    if not U_YT_AGE:
        return True, t("hc_ck_alive", _admin_lang())   # ссылку не задали — проверять нечем

    def work():
        import yt_dlp

        def op(proxy_opts: dict):
            opts = {**BASE_OPTS, "skip_download": True, "noplaylist": True,
                    **_cookie_opts(U_YT_AGE), **proxy_opts}
            return yt_dlp.YoutubeDL(opts).extract_info(U_YT_AGE, download=False)

        try:
            # Тот же путь, что у боевого скачивания: при необходимости через прокси.
            _with_music_fallback(U_YT_AGE, op)
        except Exception as e:
            if "confirm your age" in str(e).lower():
                return False, t("hc_ck_dead", _admin_lang())
            return False, f"{type(e).__name__}: {e}"[:120]
        return True, t("hc_ck_alive", _admin_lang())

    return await asyncio.to_thread(work)


async def _check_x_cookies():
    """Живы ли куки X (Twitter). Тот же смысл, что и у Instagram: без входа часть
    постов не отдаётся, но остальные проверки этого не замечают.

    Проверено опытом: с живыми куками ответ 404 (сам метод давно убран, но запрос
    опознан), с испорченными – 401, без кук вовсе – 403. Значит «401 или 403» и есть
    признак мёртвой сессии, остальное считаем рабочим.
    """
    from bot.config import X_COOKIES

    jar = _load_cookies(X_COOKIES)
    if jar is None:
        return False, t("hc_ck_missing", _admin_lang(), path=X_COOKIES)
    problem = _cookie_expiry_problem(jar, "auth_token")
    if problem:
        return False, problem
    ct0 = next((c.value for c in jar if c.name == "ct0"), "")

    def work():
        r = requests.get("https://api.x.com/1.1/account/verify_credentials.json",
                         cookies=jar, timeout=25,
                         headers={"User-Agent": _COOKIE_UA,
                                  "Authorization": f"Bearer {_X_BEARER}",
                                  "x-csrf-token": ct0})
        if r.status_code in (401, 403):
            return False, t("hc_ck_dead", _admin_lang())
        return True, t("hc_ck_alive", _admin_lang())

    return await asyncio.to_thread(work)


async def _check_tiktok_music():
    """Аудиодорожка к слайдшоу TikTok.

    Зачем отдельным пунктом: тумблер «Присылать аудио к слайдшоу» включён по умолчанию,
    а музыка берётся отдельным запросом — не тем, что отдаёт видео. Сломается он один,
    и слайдшоу продолжат приходить, все прочие пункты останутся зелёными, а музыка молча
    перестанет доходить. Ровно так у нас пряталась история с протухшими куками.
    """
    from bot.features.download.downloaders import tiktok

    def work():
        res = tiktok.download_music(U_TIKTOK_SLIDE)
        if not res:
            return False, t("hc_music_none", _admin_lang())
        path, title = res
        try:
            size = _size_of(path)
            if not size:
                return False, t("hc_empty", _admin_lang())
            return True, f"{_kb(size)}, {(title or '').strip()[:40]}"
        finally:
            _cleanup(path)

    return await asyncio.to_thread(work)


async def _check_currency():
    from bot.features.currency.rates import convert
    res = await convert(1.0, "USD", ["EUR"])
    if not res or "EUR" not in res:
        return False, "курсы недоступны"
    return True, f"1 USD = {res['EUR']:.2f} EUR"


def _judge_speech(text: str, where: str) -> tuple[bool, str]:
    """Сверяет расшифровку образца с ожидаемыми словами.

    Смысл: проверять сам факт запуска модели мало. Раньше здесь прогонялся синтетический
    тон — он ловил смерть видеокарты, но слов в нём нет, и пустой ответ считался нормой,
    поэтому поломка самого распознавания оставалась невидимой.

    Совпасть должны не все слова, а большинство: одно слово движок может расслышать
    иначе, и ронять проверку из-за этого незачем.
    """
    from bot.config import WHISPER_PROBE_WORDS, WHISPER_PROBE_MIN_HITS

    low = (text or "").lower()
    hits = [w for w in WHISPER_PROBE_WORDS if w in low]
    if len(hits) < WHISPER_PROBE_MIN_HITS:
        return False, t("hc_wh_garbled", _admin_lang(), hits=len(hits),
                        need=WHISPER_PROBE_MIN_HITS, text=(low[:60] or "—"))
    return True, f"{where}, {len(hits)}/{len(WHISPER_PROBE_WORDS)}"


def _probe_path() -> str | None:
    from bot.config import WHISPER_PROBE
    return WHISPER_PROBE if os.path.exists(WHISPER_PROBE) else None


async def _check_stt_groq():
    """Основной способ расшифровки: облако Groq. Именно его получают пользователи."""
    from bot.config import WHISPER_PROBE, GROQ_STT_MODEL
    from bot.features.transcribe.transcriber import groq_stt

    if not _probe_path():
        return False, t("hc_wh_nosample", _admin_lang(), path=WHISPER_PROBE)
    if not groq_stt.available():
        return False, t("hc_stt_nokey", _admin_lang())
    text = await asyncio.to_thread(groq_stt.transcribe, WHISPER_PROBE)
    return _judge_speech(text, GROQ_STT_MODEL)


async def _check_stt_relay():
    """Последний рубеж расшифровки: чужой бот через аккаунт-посредник.

    Проверяем только ЖИВА ЛИ СЕССИЯ, файл боту не шлём: гонять чужое голосовое через
    посторонний сервис каждые два часа незачем, а ломается тут почти всегда именно
    авторизация. Файл сессии переживает её отзыв — завершил сеансы в настройках
    Telegram, и рубеж молча мёртв, хотя на диске всё на месте. Ровно так и случилось:
    сессию закрыли, а узнали об этом только когда полезли ей пользоваться.
    """
    from bot.features.transcribe.transcriber import relay_stt
    from bot.config import RELAY_STT_BOT

    if not relay_stt.available():
        return False, t("hc_relay_nosession", _admin_lang())
    if not await relay_stt.authorized():
        return False, t("hc_relay_dead", _admin_lang())
    return True, t("hc_relay_ok", _admin_lang(), bot=RELAY_STT_BOT)


async def _check_stt_local():
    """Запасной способ: наша видеокарта. Пользователи его не видят, пока жив Groq, —
    тем важнее проверять отдельно, иначе страховка тихо сгниёт."""
    from bot.config import WHISPER_PROBE, WHISPER_DEVICE

    if not _probe_path():
        return False, t("hc_wh_nosample", _admin_lang(), path=WHISPER_PROBE)

    def work():
        import bot.features.transcribe.transcriber.whisper_transcriber as w
        from bot.config import WHISPER_PREWARM
        try:
            # _transcribe_once НЕ откатывается на CPU — проверяем именно текущее устройство.
            text = w._transcribe_once(WHISPER_PROBE)
            dev = "cpu (откат с GPU!)" if getattr(w, "_forced_cpu", False) else WHISPER_DEVICE
            return _judge_speech(text, dev)
        finally:
            # Прогрев выключен — значит держать 3.6 ГБ видеопамяти между проверками
            # незачем: проверка сама её и заняла, сама и отпускает.
            if not WHISPER_PREWARM:
                w.release_model()

    return await asyncio.to_thread(work)


# ---------------------------------------------------------------------------
# Проверки скачивания (реальными функциями бота)
# ---------------------------------------------------------------------------
async def _dl_probe(url, audio_only=False):
    """Универсальная лёгкая проверка скачивания: качаем САМЫЙ ЛЁГКИЙ формат тем же
    боевым путём (POT/маскировка/прокси), но без траты трафика на полное качество."""
    from bot.features.download.downloaders.ytdlp_wrapper import download_probe

    def work():
        path = download_probe(url, audio_only=audio_only)
        try:
            size = _size_of(path)
            return size > 0, _kb(size) if size else "файл пуст"
        finally:
            _cleanup(path)

    return await asyncio.to_thread(work)


async def _check_soundcloud_track(url: str):
    """SoundCloud тем же путём, что и у пользователя: сперва сам SoundCloud, при отказе —
    поиск того же трека на YouTube.

    Раньше проверка качала только с SoundCloud и потому врала. Официальные треки там
    закрыты защитой от копирования («This video is DRM protected»), пункт краснел — а
    человеку трек при этом приходил, потому что в боевом пути есть запасной ход через
    YouTube (см. _soundcloud_query в link.py). Проверка должна показывать то, что видит
    человек, иначе она не проверка. Заодно этот запасной ход наконец проверяется: до сих
    пор он не был покрыт вовсе и мог сгнить незаметно.
    """
    from bot.features.download.downloaders.ytdlp_wrapper import download_probe, search_audio
    from bot.features.download.link import _soundcloud_query

    def work():
        try:
            path = download_probe(url, audio_only=True)
        except Exception as e:
            # Прямой путь закрыт — идём тем же обходным, что и бот у пользователя.
            direct = f"{type(e).__name__}"
            found = search_audio(_soundcloud_query(url))
            path = download_probe(found, audio_only=True)
            try:
                size = _size_of(path)
                return size > 0, (f"через YouTube ({_kb(size)}), сам SoundCloud отказал: {direct}"
                                  if size else "файл пуст")
            finally:
                _cleanup(path)
        try:
            size = _size_of(path)
            return size > 0, _kb(size) if size else "файл пуст"
        finally:
            _cleanup(path)

    return await asyncio.to_thread(work)


def _short_side(path: str) -> int:
    """Короткая сторона кадра. Именно она задаёт «качество»: у вертикального ролика
    720x1280 это 720, а высота равна 1280 – на этом мы уже один раз обожглись, когда
    ограничение «не выше 720» срезало вертикальные шортсы до 360."""
    from bot.features.download.downloaders.video_meta import probe_video
    meta = probe_video(path)
    w, h = meta.get("width") or 0, meta.get("height") or 0
    return min(w, h) if w and h else 0


def _admin_lang() -> str:
    """Язык админских сообщений. Причины сбоев пишем сразу на нужном языке: они уходят
    только админу, и хранить их по-русски, чтобы потом переводить, смысла нет."""
    return ADMIN_LANG


def _judge_video(path: str, min_side: int = 0, max_side: int = 0) -> tuple[bool, str]:
    """Проверяет не только «файл пришёл», но и «пришло то, что заказывали».

    Обычная проверка «размер больше нуля» пропускает целый класс поломок: файл есть,
    а качество не то. Так у нас неделю жило сжатие, отдававшее 360p вместо 720p, при
    всех зелёных галочках.
    """
    size = _size_of(path)
    if not size:
        return False, t("hc_empty", _admin_lang())
    side = _short_side(path)
    if not side:
        return False, t("hc_not_video", _admin_lang(), size=_kb(size))
    if min_side and side < min_side:
        return False, t("hc_low_q", _admin_lang(), got=side, want=min_side)
    if max_side and side > max_side:
        return False, t("hc_high_q", _admin_lang(), got=side, want=max_side)
    return True, f"{_kb(size)}, {side}p"


async def _dl_shorts(url):
    """Полное качество (сжатие выключено): ловим просадку качества на ровном месте."""
    from bot.features.download.downloaders.ytdlp_wrapper import download_shorts

    def work():
        path = download_shorts(url)
        try:
            return _judge_video(path, min_side=480)
        finally:
            _cleanup(path)

    return await asyncio.to_thread(work)


async def _dl_shorts_compressed(url):
    """Тот же ролик, но со «Сжатием шортс». Проверяем, что потолок И соблюдён, И не
    перевыполнен: пришло больше заказанного – сжатие не работает, сильно меньше –
    работает неправильно (ровно этот случай мы и ловили: 360p вместо 720p)."""
    from bot.features.download.downloaders.ytdlp_wrapper import download_shorts
    from bot.config import SHORTS_CAP_HEIGHT

    cap = SHORTS_CAP_HEIGHT

    def work():
        path = download_shorts(url, max_height=cap)
        try:
            # Нижняя граница – половина потолка: откат на ступень ниже (720 → 480)
            # это нормально, а вот падение до 360 при потолке 720 уже поломка.
            return _judge_video(path, min_side=cap // 2 + 1, max_side=cap)
        finally:
            _cleanup(path)

    return await asyncio.to_thread(work)


async def _dl_pornhub_short(url):
    """PornHub shorties — это обычное видео с другим URL. Переписываем в стандартный
    (как это делает боевой обработчик) и качаем самый лёгкий формат."""
    from urllib.parse import urlparse
    from bot.features.download.downloaders.ytdlp_wrapper import download_probe

    def work():
        vid = urlparse(url).path.rstrip("/").split("/")[-1]
        std = f"https://www.pornhub.com/view_video.php?viewkey={vid}"
        path = download_probe(std)
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
    from bot.features.download.downloaders.ytdlp_wrapper import download_probe

    def work():
        info = get_track_info(U_SPOTIFY)
        if not info.get("title"):
            return False, "нет метаданных (проверь ключи Spotify)"
        label = f"{info['artist']} – {info['title']}"
        src = find_track_source(info["artist"].split(",")[0], info["title"], info["duration"])
        if not src:
            return False, f"источник не найден ({label})"
        path = download_probe(src, audio_only=True)
        try:
            size = _size_of(path)
            return size > 0, f"{label} – {_kb(size)}" if size else f"скачивание пусто ({label})"
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
            # Instagram обычно отдаёт единственный вариант, поэтому потолок не проверяем –
            # но убеждаемся, что это настоящее видео, а не битый огрызок нужного размера.
            return _judge_video(path, min_side=360)
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


async def _check_twitter(url, expect_text=False, expect_kind=None):
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
            if not size:
                return False, "медиа пусто"
            # Сверяем ВИД медиа, а не только «что-то скачалось»: у гифки своя ветка
            # отправки, и подмена её на видео прошла бы незамеченной.
            if expect_kind and expect_kind not in {f["kind"] for f in files}:
                return False, f"ожидали {expect_kind}, пришло: {kinds}"
            return True, f"{kinds}, {_kb(size)}"
        finally:
            _cleanup(paths)

    return await asyncio.to_thread(work)


async def _check_hdrezka(url):
    """HDRezka: открываем страницу и читаем инфо (озвучки/сезоны). Само видео не качаем."""
    from bot.features.download.downloaders import hdrezka

    def work():
        api = hdrezka.open_media(url)
        info = hdrezka.get_info(api, url)
        title = info.get("title") or info.get("name") or "?"
        return True, f"открыт: {title}"

    return await asyncio.to_thread(work)


# ---------------------------------------------------------------------------
# Реестр проверок. Порядок = порядок в отчёте. url="" → проверка пропускается (⚪).
# ---------------------------------------------------------------------------
# Метка «пункт выключен настройкой» вместо тестовой ссылки.
_OFF = "-off-"

_CHECKS = [
    # (название, платформа-для-порядка, проверка, тестовая ссылка). Платформа=None —
    # это функция (не площадка): такие всегда идут в конце.
    ("YouTube видео",           "YouTube",    lambda: _dl_probe(U_YT_VIDEO),               U_YT_VIDEO),
    ("YouTube Shorts",          "YouTube",    lambda: _dl_shorts(U_YT_SHORTS),             U_YT_SHORTS),
    ("YouTube Shorts (сжатие)",  "YouTube",   lambda: _dl_shorts_compressed(U_YT_SHORTS_HQ), U_YT_SHORTS_HQ),
    ("YT Music",                "YouTube",    lambda: _dl_probe(U_YT_MUSIC, True),         U_YT_MUSIC),
    ("Spotify трек",            "Spotify",    _check_spotify,                               U_SPOTIFY),
    ("Spotify альбом/плейлист", "Spotify",    _check_spotify_collection,                    U_SPOTIFY_COL),
    ("SoundCloud трек",         "SoundCloud", lambda: _check_soundcloud_track(U_SOUNDCLOUD), U_SOUNDCLOUD),
    ("SoundCloud сет",          "SoundCloud", _check_soundcloud_set,                        U_SOUNDCLOUD_SET),
    ("Instagram Reels",         "Instagram",  lambda: _dl_reel(U_IG_REEL),                 U_IG_REEL),
    ("Instagram фото-пост",     "Instagram",  lambda: _dl_ig_post(U_IG_PHOTO),             U_IG_PHOTO),
    ("Instagram карусель",      "Instagram",  lambda: _dl_ig_post(U_IG_CAROUSEL, True),    U_IG_CAROUSEL),
    ("TikTok видео",            "TikTok",     lambda: _check_tiktok(U_TIKTOK),             U_TIKTOK),
    ("TikTok слайдшоу",         "TikTok",     lambda: _check_tiktok(U_TIKTOK_SLIDE, True), U_TIKTOK_SLIDE),
    ("TikTok аудио",            "TikTok",     _check_tiktok_music,                         U_TIKTOK_SLIDE),
    ("Pinterest фото",          "Pinterest",  lambda: _dl_media(U_PINTEREST_IMG),          U_PINTEREST_IMG),
    ("Pinterest видео",         "Pinterest",  lambda: _dl_media(U_PINTEREST_VID),          U_PINTEREST_VID),
    ("Twitter видео",           "Twitter",    lambda: _check_twitter(U_TWITTER_VIDEO),     U_TWITTER_VIDEO),
    ("Twitter фото",            "Twitter",    lambda: _check_twitter(U_TWITTER_PHOTO),     U_TWITTER_PHOTO),
    ("Twitter текст",           "Twitter",    lambda: _check_twitter(U_TWITTER_TEXT, True),U_TWITTER_TEXT),
    ("Twitter GIF",             "Twitter",    lambda: _check_twitter(U_TWITTER_GIF, expect_kind="gif"), U_TWITTER_GIF),
    ("PornHub видео",           "PornHub",    lambda: _dl_probe(U_PORNHUB),                U_PORNHUB),
    ("PornHub Shorties",        "PornHub",    lambda: _dl_pornhub_short(U_PORNHUB_SHORT),  U_PORNHUB_SHORT),
    ("HDRezka фильм",           "HDRezka",    lambda: _check_hdrezka(U_HDREZKA),           U_HDREZKA),
    ("HDRezka сериал",          "HDRezka",    lambda: _check_hdrezka(U_HDREZKA_SERIES),    U_HDREZKA_SERIES),
    # Функции (не площадки) — всегда в конце
    ("ИИ-ассистент",            None,         _check_ai,                                    "x"),
    ("Конвертер валют",         None,         _check_currency,                              "x"),
    ("Расшифровка (Groq)",      None,         _check_stt_groq,                              "x"),
    # Пустая ссылка = пункт пропускается. Локальный Whisper проверяем ТОЛЬКО если он
    # реально стоит в цепочке: иначе выключенная модель всё равно поднималась бы каждые
    # два часа и держала RAM, ради экономии которой её и выключали.
    ("Расшифровка (Wisper)",     None,         _check_stt_local,
     "x" if "local" in STT_ORDER else _OFF),
    # Как и локальный Whisper — проверяем, только если способ реально стоит в цепочке.
    ("Расшифровка (альтернативная)", None,     _check_stt_relay,
     "x" if "relay" in STT_ORDER else _OFF),
    ("yt-dlp последний",        None,         _check_ytdlp,                                 "x"),
    ("Куки Instagram",          None,         _check_ig_cookies,                            "x"),
    ("Куки YouTube",            None,         _check_yt_cookies,                            "x"),
    ("Куки Twitter",            None,         _check_x_cookies,                             "x"),
]


# Проверки с Playwright (headless-браузер) гоняем строго по очереди через общий замок:
# синхронный Playwright не любит параллельный запуск. Сейчас такая одна — HDRezka
# (обход анти-бота); замок оставлен на случай появления новых.
_PLAYWRIGHT_CHECKS = {"HDRezka фильм", "HDRezka сериал"}
# TikTok API (tikwm) держит лимит «1 запрос/сек» — гоняем TikTok-чеки строго по одному
# с паузой между ними, иначе видео+слайдшоу сталкиваются и ловят «Free Api Limit».
_TIKTOK_CHECKS = {"TikTok видео", "TikTok слайдшоу", "TikTok аудиодорожка"}
# Instagram-чеки читают ОДИН файл кук; при параллельном доступе yt-dlp может писать его
# обратно и портить — ловится как «failed to load cookies». Поэтому тоже по одному.
_INSTAGRAM_CHECKS = {"Instagram Reels", "Instagram фото-пост", "Instagram карусель"}


async def _run(name: str, platform: str | None, coro_fn, url: str, idx: int,
               sem: asyncio.Semaphore, pw_lock: asyncio.Lock,
               tiktok_lock: asyncio.Lock, ig_lock: asyncio.Lock) -> dict:
    """Запускает одну проверку с таймаутом и перехватом ошибок. url=="" → пропуск.
    Упавший пункт перепроверяем один раз через паузу — тревога только при двойном сбое."""
    base = {"name": name, "platform": platform, "idx": idx}
    if url == _OFF:
        # Пункт не сломан, а сознательно выключен настройкой — так и пишем, иначе
        # «нет тестовой ссылки» выглядит как недоделка.
        return {**base, "state": "skip", "detail": t("hc_off", _admin_lang()), "sec": 0.0}
    if not url:
        return {**base, "state": "skip", "detail": t("hc_no_url", _admin_lang()), "sec": 0.0}

    async def _attempt() -> tuple[bool, str]:
        if name in _PLAYWRIGHT_CHECKS:
            async with pw_lock:                      # Playwright-проверки — строго по одной
                return await asyncio.wait_for(coro_fn(), _PROBE_TIMEOUT)
        if name in _TIKTOK_CHECKS:
            async with tiktok_lock:                  # tikwm: не больше 1 запроса/сек
                res = await asyncio.wait_for(coro_fn(), _PROBE_TIMEOUT)
                await asyncio.sleep(1.2)             # пауза перед следующим TikTok-чеком
                return res
        if name in _INSTAGRAM_CHECKS:
            async with ig_lock:                      # общий файл кук — без гонок записи
                return await asyncio.wait_for(coro_fn(), _PROBE_TIMEOUT)
        return await asyncio.wait_for(coro_fn(), _PROBE_TIMEOUT)

    async with sem:
        start = time.monotonic()
        for attempt in (1, 2):                       # первая попытка + один повтор
            try:
                ok, detail = await _attempt()
                state = "ok" if ok else "fail"
            except asyncio.TimeoutError:
                state, detail = "fail", f"таймаут > {_PROBE_TIMEOUT}с"
            except Exception as e:                   # noqa: BLE001 — отчёт важнее типа
                state, detail = "fail", f"{type(e).__name__}: {e}"[:140]
                if attempt == 2:
                    logger.warning("Проверка «%s» упала (после повтора)", name, exc_info=True)
            if state == "ok" or attempt == 2:
                break
            logger.info("Проверка «%s» упала (%s) — повтор через %dс", name, detail, _RETRY_DELAY)
            await asyncio.sleep(_RETRY_DELAY)
    return {**base, "state": state, "detail": detail,
            "sec": round(time.monotonic() - start, 1)}


async def run_health_checks() -> list[dict]:
    """Гоняет все проверки (ограничивая одновременность) и возвращает результаты по порядку."""
    sem = asyncio.Semaphore(_CONCURRENCY)
    pw_lock = asyncio.Lock()
    tiktok_lock = asyncio.Lock()
    ig_lock = asyncio.Lock()
    return list(await asyncio.gather(
        *(_run(name, platform, fn, url, idx, sem, pw_lock, tiktok_lock, ig_lock)
          for idx, (name, platform, fn, url) in enumerate(_CHECKS))))


# Кэш последней проверки: заполняется периодической проверкой (каждые N часов) и дневным
# отчётом. /statistics берёт результат отсюда и показывает время, КОГДА он был снят, —
# чтобы не гонять тяжёлую проверку на каждое нажатие.
_LAST: dict = {"results": [], "at": None}


# Во сколько раз проверка должна замедлиться, чтобы счесть это поломкой. Порог намеренно
# грубый: скорость чужих серверов гуляет сама по себе (один и тот же TikTok приходил и за
# 4с, и за 7с без единой правки), и чуткая тревога врала бы постоянно.
_SLOW_FACTOR = 3.0
# И вырасти хотя бы на столько секунд: рост «0.2с → 0.7с» формально трёхкратный, но
# пользователю незаметен и тревоги не стоит.
_SLOW_MIN_DELTA = 5.0
# Пока замеров мало, нормы ещё нет – молчим, иначе первые прогоны дадут ложь.
_SLOW_MIN_SAMPLES = 5


async def _mark_slow(results: list[dict]) -> None:
    """Сравнивает время каждой проверки с её собственной нормой и помечает замедлившиеся.

    Ловит то, о чём мы не догадались написать проверку: падение PornHub с 4с до 48с
    никто бы не предусмотрел отдельным пунктом, а отклонение от нормы видно само.
    Время берём только у успешных проверок – у сломанной оно бессмысленно.
    """
    from bot.database import SessionLocal
    from bot.database.repository import get_check_baselines, save_check_timings

    try:
        async with SessionLocal() as session:
            baselines = await get_check_baselines(session)
            for r in results:
                base, n = baselines.get(r["name"], (None, 0))
                if r["state"] != "ok" or base is None or n < _SLOW_MIN_SAMPLES:
                    continue
                if r["sec"] >= base * _SLOW_FACTOR and r["sec"] - base >= _SLOW_MIN_DELTA:
                    r["slow"] = True
                    r["baseline"] = round(base, 1)
            # Норму обновляем ПОСЛЕ сравнения, иначе текущий прогон подтянет её к себе.
            await save_check_timings(
                session, [(r["name"], r["sec"]) for r in results if r["state"] == "ok"])
    except Exception:
        logger.exception("Не удалось сверить время проверок с нормой")


# Идущий прямо сейчас прогон. Проверка тяжёлая (качает тестовые ролики со всех
# площадок), и два прогона разом мешают друг другу: нагрузка удваивается, время растёт,
# а исправные площадки начинают помечаться медленными. Раньше это было возможно —
# /statistics или /test могли стартовать поверх плановой проверки. Теперь второй
# желающий не запускает свою, а дожидается результата уже идущей.
_RUNNING: "asyncio.Task | None" = None


async def _run_and_cache() -> list[dict]:
    results = await run_health_checks()
    await _mark_slow(results)
    _LAST["results"] = results
    _LAST["at"] = datetime.now(_ADMIN_ZONE)
    return results


async def run_and_cache() -> list[dict]:
    """Прогоняет проверку и запоминает результат + время (для /statistics).

    Прогон всегда один на весь бот: если проверка уже идёт, подключаемся к ней и ждём
    её результат, а свою не запускаем.
    """
    global _RUNNING
    if _RUNNING is None or _RUNNING.done():
        _RUNNING = asyncio.create_task(_run_and_cache())
        # Снимаем ссылку сами, когда прогон закончится — хоть результатом, хоть падением.
        # Через try/finally было бы неверно: если ждущего отменят (например, админ
        # перезапустил бота посреди проверки), ссылка обнулилась бы, пока прогон ещё идёт,
        # и следующий вызов запустил бы второй параллельно — ровно то, от чего уходим.
        _RUNNING.add_done_callback(_forget_run)
    else:
        logger.info("Проверка уже идёт — жду её результат, свою не запускаю")
    # shield: отмена ожидающего не должна убивать сам прогон, его ждут и другие.
    return await asyncio.shield(_RUNNING)


def _forget_run(task: "asyncio.Task") -> None:
    global _RUNNING
    if _RUNNING is task:
        _RUNNING = None


def is_running() -> bool:
    """Идёт ли проверка прямо сейчас (для текста «уже выполняется» в /statistics)."""
    return _RUNNING is not None and not _RUNNING.done()


def last_results() -> tuple[list[dict], "datetime | None"]:
    """Последний закэшированный результат проверки и время его снятия (или [], None)."""
    return _LAST["results"], _LAST["at"]


def _display_order(results: list[dict], platform_order: list[str] | None) -> list[dict]:
    """Порядок вывода: платформы — по убыванию использования (platform_order), внутри
    платформы — по алфавиту; функции (platform=None) — в самом конце. Без platform_order
    (например, в тревоге) — оставляем порядок реестра."""
    if not platform_order:
        return results
    rank = {p: i for i, p in enumerate(platform_order)}

    def key(r):
        p = r.get("platform")
        if p is None:
            return (2, r.get("idx", 0), "")               # функции — в конце, в порядке реестра
        if p not in rank:
            return (1, 0, r["name"].lower())              # платформа без статистики — следом
        return (0, rank[p], r["name"].lower())            # по использованию, внутри — алфавит

    return sorted(results, key=key)


def format_health(results: list[dict], platform_order: list[str] | None = None,
                  at: "datetime | None" = None, lang: str = "ru") -> str:
    """Блок «проверка функционала» для отчёта (HTML). platform_order — платформы по
    убыванию использования (порядок как в «По платформам»). at — время, когда проверка
    была снята: если задано, в заголовок добавляется «(была в ЧЧ:ММ)» (для /statistics)."""
    icons = {"ok": "✅", "fail": "❌", "skip": "⚪"}
    ok_n = sum(1 for r in results if r["state"] == "ok")
    tested = sum(1 for r in results if r["state"] != "skip")
    head = t("hc_title", lang)
    if at is not None:
        head += f" ({t('hc_taken_at', lang, time=at.strftime('%H:%M'))})"
    lines = [f"<b>{head}: {ok_n}/{tested}</b>"]
    # Список прячем в раскрывающуюся цитату: пунктов три десятка, развёрнутыми они
    # занимают весь экран. Главное — счёт, подробности по нажатию.
    #
    # Открывающий тег ПРИКЛЕИВАЕМ к первой строке, а не кладём отдельным элементом:
    # иначе после join между тегом и первым пунктом появляется перевод строки, и
    # Telegram рисует внутри цитаты пустую строку сверху.
    body, problems = [], []
    for r in _display_order(results, platform_order):
        # Рабочие — чисто (только галочка + название). У сломанных/пропущенных оставляем
        # короткую причину (это не размер/время, а «что не так»), без времени.
        if r["state"] == "ok":
            # Работает, но резко медленнее своей нормы — молча пропускать такое нельзя.
            if r.get("slow"):
                line = (f"🐢 {t_check(r['name'], lang)} – "
                        f"{t('hc_slow', lang, sec=r['sec'], base=r['baseline'])}")
                problems.append(line)
            else:
                line = f"✅ {t_check(r['name'], lang)}"
        else:
            line = f"{icons[r['state']]} {t_check(r['name'], lang)} – {r['detail']}"
            # Пропущенные (⚪) в проблемы не берём: это не поломка, а сознательно
            # выключенный настройкой пункт.
            if r["state"] != "skip":
                problems.append(line)
        body.append(line)

    if body:
        body[0] = "<blockquote expandable>" + body[0]
        body[-1] = body[-1] + "</blockquote>"
    lines += body
    # Что не работает — ПОД цитатой обычным текстом: это должно быть видно сразу,
    # не разворачивая список.
    lines += problems
    return "\n".join(lines)


def _short_reason(detail: str) -> str:
    """Короткая причина сбоя для тревоги: вытаскиваем «HTTP Error NNN» или обрезаем."""
    m = re.search(r"HTTP Error \d+(?:: [\w ]+?)?(?= \(|$|\.)", detail)
    if m:
        return m.group(0).strip()
    s = detail.split(" (caused by")[0].strip()
    if len(s) > 70 and ": " in s:
        s = s.split(": ")[-1]
    return s[:80]


def format_alert(results: list[dict], lang: str = "ru") -> str:
    """Короткая тревога — про сломанное и про резко замедлившееся. Пусто, если всё в
    порядке (тогда не шлём). Замедление отдельным блоком: формально работает, но стало
    заметно хуже, и это тоже поломка — просто тихая."""
    failed = [r for r in results if r["state"] == "fail"]
    slow = [r for r in results if r.get("slow")]
    if not failed and not slow:
        return ""
    ok_n = sum(1 for r in results if r["state"] == "ok")
    tested = sum(1 for r in results if r["state"] != "skip")
    lines = [f"❌ {t_check(r['name'], lang)} - {_short_reason(r['detail'])}"
             for r in failed]
    for r in slow:
        lines.append(f"🐢 {t_check(r['name'], lang)} - "
                     f"{t('hc_slow', lang, sec=r['sec'], base=r['baseline'])}")
    lines.append(t("hc_rest_ok", lang, ok=ok_n, total=tested))
    return "\n".join(lines)
