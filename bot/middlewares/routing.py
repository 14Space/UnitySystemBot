"""
Применяет настройки /setconfig: бот умеет всё, но в конкретном чате часть функций
может быть выключена (админом группы или самим пользователем в личке). Если функция
выключена — молча пропускаем сообщение мимо хендлеров. Здесь же сообщаем репозиторию
id текущего бота: кэш file_id ведётся по боту (id чужого бота невалиден).
"""
from typing import Callable, Any
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, InlineQuery

from bot.database import SessionLocal
from bot.database.repository import get_disabled_features, current_bot_id
from bot.features.currency.parser import parse as parse_currency


def _msg_feature(m: Message) -> str | None:
    """К какой функции относится сообщение (None = команда/прочее, не гейтим)."""
    if m.voice or m.video_note:
        return "transcribe"
    text = m.text or ""
    if text.startswith("http"):
        return "download"
    if text and parse_currency(text):
        return "currency"
    return None


class RoutingMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable, event: Any, data: dict) -> Any:
        bot = getattr(event, "bot", None)
        current_bot_id.set(bot.id if bot else None)

        if isinstance(event, Message):
            feature = _msg_feature(event)
            if feature:
                async with SessionLocal() as session:
                    disabled = await get_disabled_features(session, event.chat.id)
                if feature in disabled:      # выключено в этом чате через /setconfig
                    return
        return await handler(event, data)
