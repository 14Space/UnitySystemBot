"""
Очередь запросов на пользователя.

Раньше здесь стоял обычный ограничитель частоты: не больше 5 запросов за 10 секунд,
всё сверх — ОТБРАСЫВАЛОСЬ с ответом «слишком много запросов». На пересылке пачки
голосовых это выходило боком: кидаешь десяток, пять обрабатываются, пять молча
пропадают, и человек даже не знает, какие именно.

Теперь лишнее не теряется, а ЖДЁТ. Одновременно у одного человека обрабатывается
THROTTLE_LIMIT запросов, остальные стоят в очереди и заходят по мере освобождения
мест. Очередь ограничена THROTTLE_QUEUE_MAX (по умолчанию 20 на человека): живые люди
столько подряд не присылают, а без предела один человек мог занять боту память
потоком сообщений — на это указал внешний аудит.

Очередь именно на ЧЕЛОВЕКА, а не на весь бот: иначе один пользователь с пачкой
голосовых заставил бы ждать всех остальных.
"""
import asyncio
import os
import time
from typing import Callable, Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

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
                or not event.from_user or not _is_request(event, data):
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


# --- Нажатия кнопок -------------------------------------------------------
#
# Очередь выше стоит только на СООБЩЕНИЯХ, а кнопки не ограничивались вовсе. Между
# тем под кнопками у нас выбор качества (тяжёлая загрузка), треки коллекции и выставление
# крипто-счёта: удерживая палец на кнопке, можно было наделать десятки запросов.
# Здесь не очередь, а именно частота: нажатие — вещь мгновенная, и «подожди секунду»
# тут честный ответ.
_CLICK_WINDOW = float(os.getenv("CLICK_WINDOW_SECONDS", "5"))
_CLICK_MAX = int(os.getenv("CLICK_MAX_PER_WINDOW", "10"))


class CallbackThrottleMiddleware(BaseMiddleware):
    """Не больше _CLICK_MAX нажатий от одного человека за _CLICK_WINDOW секунд."""

    def __init__(self):
        self._clicks: dict[int, list[float]] = {}

    async def __call__(self, handler: Callable, event: Any, data: dict) -> Any:
        if not isinstance(event, CallbackQuery) or not event.from_user or _CLICK_MAX <= 0:
            return await handler(event, data)
        now = time.monotonic()
        uid = event.from_user.id
        recent = [t0 for t0 in self._clicks.get(uid, []) if now - t0 < _CLICK_WINDOW]
        if len(recent) >= _CLICK_MAX:
            self._clicks[uid] = recent
            # Отвечаем всплывашкой, а не молчанием: нажатие без ответа Telegram
            # показывает как «часики», и человек жмёт ещё сильнее.
            await event.answer(t("too_many_clicks", lang_of(event.from_user)),
                               show_alert=False)
            return
        recent.append(now)
        self._clicks[uid] = recent
        # Чистим память: без этого словарь растёт по одному ключу на пользователя
        # и живёт до перезапуска.
        if len(self._clicks) > 1000:
            self._clicks = {k: v for k, v in self._clicks.items()
                            if v and now - v[-1] < _CLICK_WINDOW}
        return await handler(event, data)
