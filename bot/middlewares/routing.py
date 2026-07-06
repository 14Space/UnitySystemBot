"""
Маршрутизация в общем моторе: несколько ботов крутятся на ОДНОМ диспетчере
(роутер нельзя привязать к нескольким диспетчерам). Этот фильтр пускает к хендлерам
только то, что умеет конкретный бот, и учитывает /setconfig для бота-комбайна.

  • viaSaver     — только скачивание;
  • viaVoice     — только транскрибация;
  • viaUnity     — всё + /setconfig по группам.
"""
from typing import Callable, Any
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, InlineQuery

from bot.database import SessionLocal
from bot.database.repository import get_disabled_features

GROUP_TYPES = ("group", "supergroup")


def _msg_feature(m: Message) -> str | None:
    """К какой функции относится сообщение (None = команда/прочее, не гейтим)."""
    if m.voice or m.video_note:
        return "transcribe"
    if (m.text or "").startswith("http"):
        return "download"
    return None


class RoutingMiddleware(BaseMiddleware):
    def __init__(self, features_by_bot: dict[int, set[str]], unity_ids: set[int]):
        self.features_by_bot = features_by_bot
        self.unity_ids = unity_ids

    async def __call__(self, handler: Callable, event: Any, data: dict) -> Any:
        bot = getattr(event, "bot", None)
        bot_id = bot.id if bot else None
        feats = self.features_by_bot.get(bot_id, set())
        is_unity = bot_id in self.unity_ids

        feature = None
        chat = None

        if isinstance(event, Message):
            chat = event.chat
            if (event.text or "").startswith("/setconfig"):
                if not is_unity:      # /setconfig есть только у бота-комбайна
                    return
                return await handler(event, data)
            feature = _msg_feature(event)

        elif isinstance(event, CallbackQuery):
            if (event.data or "").startswith("cfg:"):
                if not is_unity:      # переключатели конфига — только у комбайна
                    return
                return await handler(event, data)
            # прочие callback приходят с кнопок, которые появляются только у ботов
            # с нужной функцией — отдельно гейтить не нужно
            return await handler(event, data)

        elif isinstance(event, InlineQuery):
            if "download" not in feats:
                await event.answer([], cache_time=5, is_personal=True)
                return
            return await handler(event, data)

        if feature:
            if feature not in feats:          # бот не умеет эту функцию — молчим
                return
            if is_unity and chat and chat.type in GROUP_TYPES:
                async with SessionLocal() as session:
                    disabled = await get_disabled_features(session, chat.id)
                if feature in disabled:       # выключено в этой группе через /setconfig
                    return
        return await handler(event, data)
