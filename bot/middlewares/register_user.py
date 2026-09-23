from typing import Callable, Any
from aiogram import BaseMiddleware
from aiogram.types import Message
from bot.database import SessionLocal
from bot.database.repository import get_or_create_user
from bot.middlewares.routing import is_request


class RegisterUserMiddleware(BaseMiddleware):
    """Записывает в базу тех, кто ОБРАЩАЕТСЯ к боту.

    Раньше сюда попадал каждый, кто написал в группе хоть что-нибудь: бот состоит в
    чате и видит всю переписку. Из-за этого «Пользователей: 43» означало не число
    пользователей, а число людей, попавших в поле зрения, и на каждое чужое сообщение
    в базу шёл лишний запрос.
    """

    async def __call__(self, handler: Callable, event: Message, data: dict) -> Any:
        if isinstance(event, Message) and event.from_user and is_request(event, data):
            async with SessionLocal() as session:
                user = await get_or_create_user(
                    session,
                    user_id=event.from_user.id,
                    username=event.from_user.username or "",
                    language=(event.from_user.language_code or "ru")[:2],
                )
                data["db_user"] = user
        return await handler(event, data)
