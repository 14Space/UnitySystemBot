import time

from aiogram import Router
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import Message

from bot.config import ADMIN_ID
from bot.features.download.link import INLINE_LINKS, process_link
from bot.features.common.keyboards.menu import main_menu_keyboard
from bot.utils.i18n import t, lang_of

router = Router()

# Защита от двойного тапа по инлайн-кнопке «Скачать в боте»: тот же переход в течение
# пары секунд игнорируем, иначе одна и та же ссылка качается дважды (одно скачивание
# успевает, второе часто падает с «Не удалось скачать»).
_recent_deeplinks: dict[str, float] = {}
_DEEPLINK_COOLDOWN = 5.0


def _welcome_key(features: set[str]) -> str:
    """Какое приветствие показать — под роль конкретного бота."""
    if features == {"transcribe"}:              # viaVoice
        return "welcome_voice"
    if features == {"download"}:                # viaSaver (только скачивание)
        return "welcome"
    return "welcome_unity"                      # хаб / одиночный бот «всё в одном»


def _help_key(features: set[str]) -> str:
    """Какую справку показать — под роль конкретного бота."""
    if features == {"transcribe"}:              # viaVoice
        return "help_voice"
    if features == {"download"}:                # viaSaver (только скачивание)
        return "help_saver"
    return "help_unity"                         # хаб / одиночный бот «всё в одном»


def _placeholder_key(features: set[str]) -> str:
    """Подсказка в поле ввода — под роль конкретного бота."""
    if features == {"transcribe"}:              # viaVoice
        return "kb_placeholder_voice"
    if features == {"download"}:                # viaSaver (только скачивание)
        return "kb_placeholder_saver"
    return "kb_placeholder_unity"               # хаб / одиночный бот «всё в одном»


@router.message(Command("help"))
async def cmd_help(message: Message, bot_features: set[str] = frozenset()):
    lang = lang_of(message.from_user)
    text = t(_help_key(bot_features), lang)
    # Админу дописываем его команды (обычным пользователям их не показываем)
    if ADMIN_ID and message.from_user.id == ADMIN_ID:
        text += t("help_admin_extra", lang)
    await message.answer(text, parse_mode="HTML")


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject,
                    bot_features: set[str] = frozenset()):
    # Переход из inline («Скачать в боте»): /start dl<id> → качаем ссылку
    payload = command.args or ""
    if payload.startswith("dl"):
        sid = payload[2:]
        now = time.monotonic()
        if now - _recent_deeplinks.get(sid, 0.0) < _DEEPLINK_COOLDOWN:
            return  # повторный тап по той же кнопке — не качаем второй раз
        _recent_deeplinks[sid] = now
        url = INLINE_LINKS.get(sid)
        if url:
            await process_link(message, url)
            return

    lang = lang_of(message.from_user)
    placeholder = t(_placeholder_key(bot_features), lang)
    await message.answer(
        t(_welcome_key(bot_features), lang),
        reply_markup=main_menu_keyboard(message.from_user.id, placeholder),
    )
