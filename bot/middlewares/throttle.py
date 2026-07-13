import time
from typing import Callable, Any
from aiogram import BaseMiddleware
from aiogram.types import Message
from bot.middlewares.routing import _msg_feature
from bot.utils.i18n import t, lang_of

# Не больше LIMIT сообщений за WINDOW секунд от одного пользователя.
WINDOW = 10.0
LIMIT = 5


def _is_request(m: Message) -> bool:
    """Троттлим только реальные запросы к боту: команды, ссылки, голосовые/кружки,
    валютные запросы. Обычную переписку (в т.ч. пересланные сообщения) — не считаем,
    иначе бот влезает с «слишком много запросов» в чужой флуд."""
    if (m.text or "").startswith("/"):
        return True
    return _msg_feature(m) is not None


class ThrottleMiddleware(BaseMiddleware):
    """Простая защита от флуда: ограничивает частоту запросов на пользователя."""

    def __init__(self):
        self._hits: dict[int, list[float]] = {}
        self._warned: set[int] = set()

    async def __call__(self, handler: Callable, event: Message, data: dict) -> Any:
        if isinstance(event, Message) and event.from_user and _is_request(event):
            uid = event.from_user.id
            now = time.monotonic()
            hits = [t for t in self._hits.get(uid, []) if now - t < WINDOW]

            if len(hits) >= LIMIT:
                # Превышен лимит — глушим, но один раз предупреждаем
                self._hits[uid] = hits
                if uid not in self._warned:
                    self._warned.add(uid)
                    await event.answer(t("too_many_requests", lang_of(event.from_user)))
                return  # сообщение не обрабатываем

            hits.append(now)
            self._hits[uid] = hits
            self._warned.discard(uid)

        return await handler(event, data)
