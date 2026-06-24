import time
from typing import Callable, Any
from aiogram import BaseMiddleware
from aiogram.types import Message

# Не больше LIMIT сообщений за WINDOW секунд от одного пользователя.
WINDOW = 10.0
LIMIT = 5


class ThrottleMiddleware(BaseMiddleware):
    """Простая защита от флуда: ограничивает частоту сообщений на пользователя."""

    def __init__(self):
        self._hits: dict[int, list[float]] = {}
        self._warned: set[int] = set()

    async def __call__(self, handler: Callable, event: Message, data: dict) -> Any:
        if isinstance(event, Message) and event.from_user:
            uid = event.from_user.id
            now = time.monotonic()
            hits = [t for t in self._hits.get(uid, []) if now - t < WINDOW]

            if len(hits) >= LIMIT:
                # Превышен лимит — глушим, но один раз предупреждаем
                self._hits[uid] = hits
                if uid not in self._warned:
                    self._warned.add(uid)
                    await event.answer("⏳ Слишком много запросов, подожди немного.")
                return  # сообщение не обрабатываем

            hits.append(now)
            self._hits[uid] = hits
            self._warned.discard(uid)

        return await handler(event, data)
