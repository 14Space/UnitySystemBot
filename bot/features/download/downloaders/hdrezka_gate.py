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
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

# Один и тот же User-Agent и в браузере (который проходит проверку), и в запросах
# библиотеки: пропуск Anubis привязан к UA, иначе куки не подойдут.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Сколько секунд считать добытые куки годными, прежде чем пройти проверку заново.
_TTL = 30 * 60

# Сколько ждать авто-решения головоломки браузером (обычно 1–3 с, берём с запасом).
_SOLVE_TIMEOUT = 30

_lock = threading.Lock()
_pw = None
_browser = None
_cookies: dict | None = None
_cookies_ts = 0.0


def _get_browser():
    """Лениво запускает один общий headless-Chromium и переиспользует его."""
    global _pw, _browser
    if _browser is None or not _browser.is_connected():
        from playwright.sync_api import sync_playwright
        if _pw is None:
            _pw = sync_playwright().start()
        # --no-sandbox обязателен под root в контейнере
        _browser = _pw.chromium.launch(args=["--no-sandbox"])
    return _browser


def _solve(url: str) -> dict:
    """Открывает страницу браузером, ждёт прохождения проверки и возвращает куки."""
    browser = _get_browser()
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


def get_cookies(url: str, force: bool = False) -> dict:
    """Возвращает куки-пропуск для HDRezka. Проходит проверку не чаще раза в TTL.
    force=True — пройти заново прямо сейчас (например, куки протухли)."""
    global _cookies, _cookies_ts
    with _lock:
        fresh = _cookies is not None and (time.time() - _cookies_ts) < _TTL
        if force or not fresh:
            try:
                _cookies = _solve(url)
                _cookies_ts = time.time()
                logger.info("HDRezka: проверка пройдена, куки обновлены (%d шт.)", len(_cookies))
            except Exception:
                logger.exception("HDRezka: не удалось пройти анти-бот-проверку")
                if _cookies is None:
                    raise
        return dict(_cookies)
