"""
«Привратник» HDRezka: обходит анти-бот-защиту сайта и добывает куки-пропуск.

Зачем. rezka.ag поставил на входе проверку Anubis («Проверяем, что вы не бот!») —
страницу-головоломку с proof-of-work на JavaScript. Обычный запрос (requests) её
не проходит и получает пустую страницу, из-за чего библиотека HdRezkaApi падает.

Как. Открываем страницу настоящим headless-браузером (Playwright — он уже есть в
проекте для карточек X). Браузер сам выполняет JS-проверку и получает cookie-
пропуск (`techaro.lol-anubis-auth`). Забираем куки и отдаём их библиотеке —
дальше она ходит по сайту обычными запросами уже как «проверенный» клиент.

Куки общие для всего домена и живут какое-то время, поэтому проходим проверку
один раз и переиспользуем куки (TTL ниже), а браузер держим одним общим
экземпляром — как в renderer/tweet_card.

Пропуск обновляется ЗАРАНЕЕ, фоновой задачей (warm), а не тем, кто первым пришёл
после истечения TTL. Причина: головоломку решает браузер на ОДНОМ общем потоке
Playwright, где стоят ещё карточки X и фото Instagram. Сама по себе она решается
за пару секунд, но в час общей проверки очередь на этом потоке растягивает её
до десятков секунд — 21.09.2026 «HDRezka сериал» занял 42.6с вместо 1.6с. Пока
пропуск свежий, браузер в этом пути не участвует вовсе.
"""
import logging
import threading
import time

from bot.utils import pw_thread

logger = logging.getLogger(__name__)

# Один и тот же User-Agent и в браузере (который проходит проверку), и в запросах
# библиотеки: пропуск Anubis привязан к UA, иначе куки не подойдут.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Сколько секунд считать добытые куки годными, прежде чем пройти проверку заново.
_TTL = 30 * 60

# Сколько ждать авто-решения головоломки браузером (обычно 1–3 с, берём с запасом).
_SOLVE_TIMEOUT = 30

# За сколько до конца TTL обновляем пропуск фоном. Пять минут — с запасом на то,
# что поток Playwright может быть занят карточкой X или фото Instagram.
_WARM_MARGIN = 5 * 60
# Страница для фонового обновления: куки общие для домена, поэтому годится корень.
_WARM_URL = "https://rezka.ag/"

_lock = threading.Lock()
_cookies: dict | None = None
_cookies_ts = 0.0


def _solve(browser, url: str) -> dict:
    """Открывает страницу общим браузером, ждёт прохождения проверки и возвращает куки."""
    context = browser.new_context(user_agent=USER_AGENT)
    try:
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # Страница-заглушка называется «Проверяем, что вы не бот!». Ждём, пока
        # браузер решит головоломку и заголовок сменится на настоящий.
        deadline = time.time() + _SOLVE_TIMEOUT
        while time.time() < deadline:
            title = (page.title() or "").lower()
            if "бот" not in title and "moment" not in title and "attention" not in title:
                break
            time.sleep(1.0)
        cookies = {c["name"]: c["value"] for c in context.cookies()}
        return cookies
    finally:
        context.close()


def warm(url: str = _WARM_URL) -> bool:
    """Обновляет пропуск заранее. True — обновили, False — не вышло (не беда).

    Головоломку решаем БЕЗ общего замка: пока идёт решение, те, кто пришёл за куками,
    продолжают пользоваться прежними. Замок берём только на подмену — она мгновенная.
    """
    global _cookies, _cookies_ts
    try:
        cookies = pw_thread.run_with_browser(_solve, url)
    except Exception:
        logger.exception("HDRezka: фоновое обновление пропуска не удалось")
        return False
    with _lock:
        _cookies, _cookies_ts = cookies, time.time()
    logger.info("HDRezka: пропуск обновлён заранее (%d кук)", len(cookies))
    return True


def warm_interval() -> int:
    """Через сколько секунд фоновой задаче обновлять пропуск."""
    return max(60, _TTL - _WARM_MARGIN)


def get_cookies(url: str, force: bool = False) -> dict:
    """Возвращает куки-пропуск для HDRezka. Проходит проверку не чаще раза в TTL.
    force=True — пройти заново прямо сейчас (например, куки протухли)."""
    global _cookies, _cookies_ts
    with _lock:
        fresh = _cookies is not None and (time.time() - _cookies_ts) < _TTL
        if force or not fresh:
            try:
                # Playwright — строго на выделенном потоке с ОБЩИМ браузером (иначе
                # greenlet-ошибка при обращении с другого потока, либо конфликт event loop
                # при втором sync_playwright() на том же потоке).
                _cookies = pw_thread.run_with_browser(_solve, url)
                _cookies_ts = time.time()
                logger.info("HDRezka: проверка пройдена, куки обновлены (%d шт.)", len(_cookies))
            except Exception:
                logger.exception("HDRezka: не удалось пройти анти-бот-проверку")
                if _cookies is None:
                    raise
        return dict(_cookies)
