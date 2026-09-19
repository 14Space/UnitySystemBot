"""
Очередь запросов на пользователя.

Раньше здесь стоял обычный ограничитель частоты: не больше 5 запросов за 10 секунд,
всё сверх — ОТБРАСЫВАЛОСЬ с ответом «слишком много запросов». На пересылке пачки
голосовых это выходило боком: кидаешь десяток, пять обрабатываются, пять молча
пропадают, и человек даже не знает, какие именно.

Теперь лишнее не теряется, а ЖДЁТ. Одновременно у одного человека обрабатывается
THROTTLE_LIMIT запросов, остальные стоят в очереди и заходят по мере освобождения
мест. По умолчанию очередь НЕ ограничена: сколько прислали, столько и обработаем, ничего
не теряется. Ограничитель THROTTLE_QUEUE_MAX остался на случай, если однажды
понадобится защита от совсем ненормального потока, но по умолчанию выключен (0).

Очередь именно на ЧЕЛОВЕКА, а не на весь бот: иначе один пользователь с пачкой
голосовых заставил бы ждать всех остальных.
"""
import asyncio
from typing import Callable, Any

from aiogram import BaseMiddleware
from aiogram.types import Message

from bot.config import THROTTLE_LIMIT, THROTTLE_QUEUE_MAX
from bot.middlewares.routing import is_request as _is_request
from bot.utils.i18n import t, lang_of


class ThrottleMiddleware(BaseMiddleware):
    """Не больше THROTTLE_LIMIT запросов от одного человека в работе одновременно;
    остальные ждут очереди, а не теряются."""

    def __init__(self):
        self._gates: dict[int, asyncio.Semaphore] = {}
        self._waiting: dict[int, int] = {}
        self._warned: set[int] = set()

    def _gate(self, uid: int) -> asyncio.Semaphore:
        gate = self._gates.get(uid)
        if gate is None:
            gate = asyncio.Semaphore(THROTTLE_LIMIT)
            self._gates[uid] = gate
        return gate

    async def __call__(self, handler: Callable, event: Message, data: dict) -> Any:
        if THROTTLE_LIMIT <= 0 or not isinstance(event, Message) \
                or not event.from_user or not _is_request(event):
            return await handler(event, data)

        uid = event.from_user.id
        waiting = self._waiting.get(uid, 0)
        if THROTTLE_QUEUE_MAX and waiting >= THROTTLE_QUEUE_MAX:
            # Это уже не пачка, а флуд: предупреждаем один раз и дальше молчим,
            # чтобы не отвечать на каждое сообщение из потока.
            if uid not in self._warned:
                self._warned.add(uid)
                await event.answer(t("too_many_requests", lang_of(event.from_user)))
            return

        self._waiting[uid] = waiting + 1
        try:
            async with self._gate(uid):
                self._warned.discard(uid)
                return await handler(event, data)
        finally:
            left = self._waiting.get(uid, 1) - 1
            if left > 0:
                self._waiting[uid] = left
            else:
                # Человек разгрёбся — не держим за ним ничего: у бота могут быть
                # тысячи пользователей, и словарь иначе растёт без предела.
                self._waiting.pop(uid, None)
                self._gates.pop(uid, None)
                self._warned.discard(uid)
