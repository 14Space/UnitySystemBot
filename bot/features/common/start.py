from aiogram import Router
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import Message

from bot.config import ADMIN_ID
from bot.features.download.link import INLINE_LINKS, process_link
from bot.features.common.keyboards.menu import main_menu_keyboard
from bot.utils.i18n import t, lang_of

router = Router()


@router.message(Command("help"))
async def cmd_help(message: Message):
    lang = lang_of(message.from_user)
    text = t("help", lang)
    # Админу дописываем его команды (обычным пользователям их не показываем)
    if ADMIN_ID and message.from_user.id == ADMIN_ID:
        text += t("help_admin_extra", lang)
    await message.answer(text, parse_mode="HTML")


def _welcome_key(features: set[str]) -> str:
    """Какое приветствие показать — под роль конкретного бота."""
    if features == {"transcribe"}:              # viaVoice
        return "welcome_voice"
    if features == {"download"}:                # viaSaver (только скачивание)
        return "welcome"
    return "welcome_unity"                      # хаб / одиночный бот «всё в одном»


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject,
                    bot_features: set[str] = frozenset()):
    # Переход из inline («Скачать в боте»): /start dl<id> → качаем ссылку
    payload = command.args or ""
    if payload.startswith("dl"):
        url = INLINE_LINKS.get(payload[2:])
        if url:
            await process_link(message, url)
            return

    lang = lang_of(message.from_user)
    await message.answer(
        t(_welcome_key(bot_features), lang),
        reply_markup=main_menu_keyboard(message.from_user.id),
    )
