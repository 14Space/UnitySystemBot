from typing import Callable, Any
from aiogram import BaseMiddleware
from aiogram.types import Message
from bot.database import SessionLocal
from bot.database.repository import get_or_create_user


class RegisterUserMiddleware(BaseMiddleware):
    """При каждом сообщении регистрирует пользователя в БД если его ещё нет"""

    async def __call__(self, handler: Callable, event: Message, data: dict) -> Any:
        if isinstance(event, Message) and event.from_user:
            async with SessionLocal() as session:
                user = await get_or_create_user(
                    session,
                    user_id=event.from_user.id,
                    username=event.from_user.username or "",
                    language=(event.from_user.language_code or "ru")[:2],
                )
                data["db_user"] = user
        return await handler(event, data)
