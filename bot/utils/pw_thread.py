"""
Единый поток и единый браузер для Playwright.

Playwright sync API привязан к потоку, который его создал: если объекты браузера дёргать
из ДРУГОГО потока, получаем `greenlet.error: Cannot switch to a different thread`. А бот
запускает синхронный код через asyncio.to_thread — это ПУЛ потоков, и разные вызовы
попадают на разные потоки. Поэтому весь Playwright-код (HDRezka-гейт, рендер карточек X,
Instagram-фото) выполняем на ОДНОМ выделенном потоке через однопоточный executor.

Вдобавок `sync_playwright()` можно поднять на потоке лишь ОДИН раз: он держит свой
event loop в фоне, и вторая попытка стартовать sync-Playwright на том же потоке падает
с «Playwright Sync API inside the asyncio loop». Поэтому держим ОДИН общий экземпляр
Playwright и ОДИН общий браузер на всех потребителей (см. run_with_browser).
"""
import concurrent.futures

# Ровно один рабочий поток — все задачи Playwright исполняются строго на нём.
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")

_playwright = None   # единственный sync_playwright().start() на весь поток
_browser = None      # единственный headless-Chromium, общий для всех потребителей


def _shared_browser():
    """Возвращает общий headless-браузер, поднимая Playwright и браузер по необходимости.
    Вызывается ТОЛЬКО изнутри выделенного потока (внутри run/run_with_browser)."""
    global _playwright, _browser
    from playwright.sync_api import sync_playwright
    if _playwright is None:
        _playwright = sync_playwright().start()
    if _browser is None or not _browser.is_connected():
        # --no-sandbox обязателен под root в контейнере
        _browser = _playwright.chromium.launch(args=["--no-sandbox"])
    return _browser


def run(fn, *args, **kwargs):
    """Выполняет fn на едином Playwright-потоке и возвращает результат (или пробрасывает
    исключение). Вызывать из рабочих потоков (например, изнутри asyncio.to_thread), НЕ из
    самого Playwright-потока — иначе однопоточный executor заблокирует сам себя."""
    return _executor.submit(fn, *args, **kwargs).result()


def run_with_browser(fn, *args, **kwargs):
    """Как run(), но передаёт первым аргументом ОБЩИЙ браузер: fn(browser, *args, **kwargs).
    Так все потребители работают с одним экземпляром Playwright/браузера на потоке."""
    def _job():
        return fn(_shared_browser(), *args, **kwargs)
    return _executor.submit(_job).result()
