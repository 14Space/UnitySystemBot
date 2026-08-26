"""
Единый поток для Playwright.

Playwright sync API привязан к потоку, который его создал: если объекты браузера
дёргать из ДРУГОГО потока, получаем `greenlet.error: Cannot switch to a different
thread`. А бот запускает синхронный код через asyncio.to_thread — это ПУЛ потоков,
и разные вызовы попадают на разные потоки. Поэтому общий браузер, созданный на одном
потоке пула, ломается при обращении с другого.

Решение: весь Playwright-код (HDRezka-гейт, рендер карточек X) выполняем на ОДНОМ
выделенном потоке через этот однопоточный executor — тогда браузер всегда создаётся
и используется на одном и том же потоке.
"""
import concurrent.futures

# Ровно один рабочий поток — все задачи Playwright исполняются строго на нём.
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")


def run(fn, *args, **kwargs):
    """Выполняет fn на едином Playwright-потоке и возвращает результат (или пробрасывает
    исключение). Вызывать из рабочих потоков (например, изнутри asyncio.to_thread), НЕ из
    самого Playwright-потока — иначе однопоточный executor заблокирует сам себя."""
    return _executor.submit(fn, *args, **kwargs).result()
