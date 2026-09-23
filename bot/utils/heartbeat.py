"""
«Пульс» бота: признак того, что он не просто запущен, а действительно работает.

Docker умеет перезапускать УПАВШИЙ процесс, но не зависший. А зависнуть есть чему:
один заблокированный вызов внутри asyncio останавливает весь бот целиком — процесс
жив, память на месте, в логах тишина, и на сообщения он больше не отвечает. Раньше
такое чинилось только руками и только после того, как кто-то заметит.

Как устроено. Основной цикл раз в полминуты отмечает время в файле — это и есть
пульс. Проверяют его двое:

  • сторож в ОТДЕЛЬНОМ ПОТОКЕ. Поток живёт вне asyncio, поэтому продолжает работать,
    даже когда цикл встал намертво. Не увидел свежей отметки — завершает процесс, и
    Docker (restart: unless-stopped) поднимает контейнер заново.
  • сам Docker через HEALTHCHECK — читает тот же файл и красит контейнер в unhealthy.
    Это не перезапуск, а видимость: «docker ps» сразу показывает, что здесь беда.

Порог намеренно большой: бот может честно молчать, пока качает двухгигабайтный фильм.
Ложный перезапуск посреди работы хуже, чем лишние пять минут простоя.
"""
import logging
import os
import threading
import time

from bot.config import HEARTBEAT_FILE, HEARTBEAT_STALE_AFTER

logger = logging.getLogger(__name__)

# Файл с отметкой. В контейнере это временная папка, которая живёт ровно столько же,
# сколько сам контейнер, — то есть перезапуск всегда начинается с чистого листа.
PATH = HEARTBEAT_FILE

# Как часто основной цикл ставит отметку.
BEAT_EVERY = 30
# Сколько тишины считаем зависанием.
STALE_AFTER = HEARTBEAT_STALE_AFTER
# Как часто сторож смотрит на отметку.
_WATCH_EVERY = 60


def touch() -> None:
    """Ставит отметку «я жив» текущим временем."""
    try:
        with open(PATH, "w") as f:
            f.write(str(int(time.time())))
    except OSError:
        logger.warning("Не удалось записать пульс в %s", PATH, exc_info=True)


def age() -> float | None:
    """Сколько секунд назад была отметка (None — отметки ещё нет)."""
    try:
        with open(PATH) as f:
            return time.time() - float(f.read().strip())
    except (OSError, ValueError):
        return None


async def beat() -> None:
    """Фоновая задача: ставит отметку, пока жив основной цикл."""
    import asyncio
    touch()
    while True:
        await asyncio.sleep(BEAT_EVERY)
        touch()


def start_watchdog() -> None:
    """Запускает сторожа в отдельном потоке (daemon — не мешает штатному выходу)."""
    def watch():
        while True:
            time.sleep(_WATCH_EVERY)
            seconds = age()
            if seconds is None or seconds < STALE_AFTER:
                continue
            logger.critical(
                "Бот не подаёт признаков жизни %.0f секунд — перезапускаюсь", seconds)
            # Жёсткий выход: цикл завис, попросить его по-хорошему уже нельзя.
            # Код 1, чтобы перезапуск был виден как аварийный, а не штатный.
            os._exit(1)

    threading.Thread(target=watch, name="heartbeat-watchdog", daemon=True).start()
