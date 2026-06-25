from aiogram import Router
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message

from bot.handlers.link import INLINE_LINKS, process_link
from bot.utils.i18n import t, lang_of

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    # Переход из inline («Скачать в боте»): /start dl<id> → качаем ссылку
    payload = command.args or ""
    if payload.startswith("dl"):
        url = INLINE_LINKS.get(payload[2:])
        if url:
            await process_link(message, url)
            return

    await message.answer(t("welcome", lang_of(message.from_user)))
