"""
Всё про сеть в одном месте: чей это адрес, через что идти и как скачать файл.

Раньше каждое из этого было написано по нескольку раз. Логика прокси жила в трёх
загрузчиках (yt-dlp, TikTok, Instagram) и в проверке функционала, и каждый читал свою
переменную окружения по-своему. Проверка «ссылка ведёт на нашу площадку» была
аккуратной в одном месте и подстрокой («youtube.com» in url) в другом.

Скачивание файла НА ДИСК, а не в память.

Зачем. По всему проекту files качались одинаково: `requests.get(...).content`, то есть
файл целиком оказывался в оперативной памяти, и только потом записывался на диск.
Для картинки это неважно, а для видео — уже нет: у бота 7 ГБ на всё, лёгких загрузок
одновременно шесть, и каждая держала свой файл в памяти дважды (байты плюс запись).
Внешний аудит нашёл десяток таких мест. Заодно там не было предела размера: сколько
площадка отдала, столько и приняли — а Telegram больше 2 ГБ всё равно не пропустит.

Здесь один загрузчик на всех: читает поток кусками, пишет сразу в файл, обрывает
скачивание при превышении предела и убирает за собой недокачанное.
"""
import logging
import os
from urllib.parse import urlparse

import requests

from bot import config
from bot.utils import files
from bot.utils.limits import FileTooLargeError, MAX_FILE_BYTES

logger = logging.getLogger(__name__)


# --- Чей это адрес ---------------------------------------------------------
#
# Смотрим на ХОЗЯИНА адреса (hostname), а не на netloc, и сверяем его целиком, а не
# по подстроке. Разница не теоретическая: в netloc попадает и логин, поэтому ссылка
# «https://rezka.ag@evil.com/» проходила проверку «rezka в домене», а открывался по
# ней evil.com. Дальше такой адрес уходил в yt-dlp (он умеет качать с любого сайта),
# в разворачиватель коротких ссылок и мог уехать через домашний туннель – вместе с
# куками площадки.
#
# Правило: хозяин совпадает с доменом целиком либо является его поддоменом. Ссылки с
# логином/паролем и с нестандартным портом не берём вовсе – в живых ссылках площадок
# такого не бывает, а у поддельных это главный приём.
_STD_PORTS = (None, 80, 443)


def host_of(url_or_parsed) -> str:
    """Хозяин адреса в нижнем регистре; «» – если адрес подозрительный."""
    parsed = (url_or_parsed if hasattr(url_or_parsed, "hostname")
              else urlparse(url_or_parsed or ""))
    if parsed.username or parsed.password:
        return ""
    try:
        if parsed.port not in _STD_PORTS:
            return ""
    except ValueError:              # порт не число – такую ссылку тоже не берём
        return ""
    return (parsed.hostname or "").lower().rstrip(".")


def on_domain(host: str, *domains: str) -> bool:
    """Хозяин – это один из доменов целиком или его поддомен."""
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)


def url_on(url: str, *domains: str) -> bool:
    """Ссылка действительно ведёт на один из этих доменов (а не просто их упоминает)."""
    return on_domain(host_of(url), *domains)


# --- Через что идти --------------------------------------------------------
#
# Прокси – это домашний туннель. Он нужен там, где адрес сервера у площадки на
# подозрении (YouTube, TikTok-сервис), или где сессию с куками можно показывать
# только из дома (Instagram, YouTube). Значения берём из config и держим здесь
# атрибутами модуля, чтобы тесты подменяли их в одном месте.
PROXY_URL = config.PROXY_URL
INSTAGRAM_PROXY = config.INSTAGRAM_PROXY
PROXY_FIRST = config.PROXY_FIRST
PROXY_SOCKET_TIMEOUT = config.PROXY_SOCKET_TIMEOUT


def proxy_for(platform: str | None = None) -> str:
    """Адрес прокси для площадки («» – прокси нет).

    У Instagram может быть свой туннель (INSTAGRAM_PROXY). Если он не задан, берём
    общий: запрос с куками обязан идти через дом, а общий туннель – тоже дом.
    Раньше загрузчик и проверка кук решали это по-разному: проверка откатывалась на
    общий прокси, а загрузчик шёл с куками напрямую из дата-центра.
    """
    if platform == "instagram":
        return INSTAGRAM_PROXY or PROXY_URL
    return PROXY_URL


def follow_redirects(url: str, *domains: str, hops: int = 5, timeout: int = 15) -> str:
    """Разворачивает короткую ссылку (pin.it и т.п.), идя по переходам САМИ.

    На каждом шаге проверяем, что следующий адрес всё ещё на одном из доменов, и
    только тогда идём дальше. Если бы по переходам шёл requests, запрос на чужой
    адрес (в том числе внутренний) ушёл бы раньше, чем мы успели бы его проверить.
    Бросает ValueError, если цепочка уводит с разрешённых доменов.
    """
    target = url
    for _ in range(hops):
        r = session().get(target, timeout=timeout, allow_redirects=False,
                          headers={"User-Agent": "Mozilla/5.0"})
        nxt = r.headers.get("Location")
        r.close()
        if not nxt:
            return target
        nxt = requests.compat.urljoin(target, nxt)
        if not url_on(nxt, *domains):
            raise ValueError(f"переход уводит на чужой адрес: {nxt[:80]}")
        target = nxt
    return target


def as_requests(proxy: str) -> dict | None:
    """Прокси в виде, который понимает requests (None – без прокси)."""
    return {"http": proxy, "https": proxy} if proxy else None


def with_proxy(op, proxy: str, *, first: bool | None = None):
    """Выполняет op(proxy) напрямую, а при ошибке – через прокси.

    op получает строку прокси: «» – прямой заход. Прокси не задан – один прямой
    вызов. first=True (или настройка PROXY_FIRST) – сразу через прокси: на сервере,
    чей адрес площадка уже забанила, прямая попытка обречена и только съедает время.

    Порядок «сперва напрямую» – потому что дома и на чистом адресе прокси не нужен и
    только замедлил бы.
    """
    if not proxy:
        return op("")
    if PROXY_FIRST if first is None else first:
        return op(proxy)
    try:
        return op("")
    except Exception:
        logger.info("Напрямую не вышло – пробую через прокси")
        return op(proxy)

# Размер куска. 256 КБ — обычный компромисс: системных вызовов немного, а памяти
# держим на порядки меньше, чем весит сам файл.
_CHUNK = 256 * 1024


def fetch_to_file(url: str, path: str, *, max_bytes: int = MAX_FILE_BYTES,
                  headers: dict | None = None, proxies: dict | None = None,
                  timeout: int = 60, session: requests.Session | None = None,
                  progress=None, total: int = 0) -> str:
    """Качает url в файл path и возвращает путь.

    progress(percent) – зовётся по ходу, шагом в 5%, если размер известен: его сообщил
    сервер или передал вызывающий (total). Так качает HDRezka – фильм идёт минутами,
    и человек должен видеть полоску.

    Бросает FileTooLargeError, если файл больше max_bytes — ПО ХОДУ скачивания, а не
    после: смысл предела в том, чтобы не тратить час и гигабайты канала на файл,
    который всё равно не отправить. Недокачанное удаляем, чтобы на диске не оставалось
    обрубков, которые потом выглядят как «битый файл».
    """
    getter = session.get if session is not None else requests.get
    written = 0
    try:
        with getter(url, headers=headers, proxies=proxies, timeout=timeout,
                    stream=True) as r:
            r.raise_for_status()
            # Если сервер честно сказал размер — отказываемся сразу, не качая.
            declared = int(r.headers.get("Content-Length") or 0)
            if max_bytes and declared > max_bytes:
                raise FileTooLargeError(
                    f"{declared / 1024 ** 3:.1f} ГБ — больше предела "
                    f"{max_bytes / 1024 ** 3:.1f} ГБ")
            total = total or declared
            last = -5
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                for chunk in r.iter_content(_CHUNK):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if max_bytes and written > max_bytes:
                        raise FileTooLargeError(
                            f"больше предела {max_bytes / 1024 ** 3:.1f} ГБ")
                    f.write(chunk)
                    if progress and total:
                        percent = min(100, round(written / total * 100))
                        if percent >= last + 5:
                            last = percent
                            progress(percent)
    except Exception:
        _drop(path)
        raise
    if written == 0:
        _drop(path)
        raise RuntimeError(f"пустой ответ: {url[:80]}")
    return path


def fetch_bytes(url: str, *, max_bytes: int = 20 * 1024 ** 2,
                headers: dict | None = None, proxies: dict | None = None,
                timeout: int = 30) -> bytes:
    """Небольшой ответ целиком в память – для обложек и превью, которые всё равно
    разбираются в памяти (Pillow). Отличие от `requests.get(...).content` – предел
    размера: вместо картинки по ссылке может приехать что угодно и сколько угодно.
    """
    chunks, got = [], 0
    with session().get(url, headers=headers, proxies=proxies, timeout=timeout,
                       stream=True) as r:
        r.raise_for_status()
        if int(r.headers.get("Content-Length") or 0) > max_bytes:
            raise FileTooLargeError(f"ответ больше {max_bytes // 1024 ** 2} МБ: {url[:80]}")
        for chunk in r.iter_content(_CHUNK):
            got += len(chunk)
            if got > max_bytes:
                raise FileTooLargeError(f"ответ больше {max_bytes // 1024 ** 2} МБ: {url[:80]}")
            chunks.append(chunk)
    return b"".join(chunks)


def _drop(path: str) -> None:
    files.remove(path)


# Общая сессия requests: держит TLS-соединения открытыми между запросами. Без неё
# каждый вызов заново договаривается о шифровании — это отдельный круг до сервера и
# обратно. Больнее всего там, где запросы частые: опрос платежей раз в 15 секунд и
# обращения к API TikTok на каждую ссылку.
_session: requests.Session | None = None


def session() -> requests.Session:
    """Общая сессия. Создаётся при первом обращении и живёт до конца процесса."""
    global _session
    if _session is None:
        _session = requests.Session()
        # Пул под наши лимиты: одновременных загрузок до восьми, плюс запас.
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
        _session.mount("https://", adapter)
        _session.mount("http://", adapter)
    return _session
