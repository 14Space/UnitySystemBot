"""
Команда /setconfig для @viaUnitySystem: администратор группы включает/выключает
функции бота в этой группе (транскрибация, скачивание, позже — конвертация валют).
Подключается только к боту-комбайну (with_config=True), обычных ботов не касается.
"""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.database import SessionLocal
from bot.database.repository import get_disabled_features, set_feature
from bot.utils.i18n import t, lang_of

router = Router()

GROUP_TYPES = ("group", "supergroup")

# Функции, которые можно переключать: (внутреннее имя, ключ подписи).
# Currency добавим, когда реализуем конвертер.
TOGGLEABLE = [
    ("transcribe", "cfg_transcribe"),
    ("download", "cfg_download"),
]


async def _is_admin(bot, chat_id: int, user_id: int) -> bool:
    """True, если пользователь — создатель или админ группы."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


def _keyboard(disabled: set[str], lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for feature, label_key in TOGGLEABLE:
        mark = "❌" if feature in disabled else "✅"
        builder.button(text=f"{mark} {t(label_key, lang)}", callback_data=f"cfg:{feature}")
    builder.adjust(1)
    return builder.as_markup()


@router.message(Command("setconfig"))
async def cmd_setconfig(message: Message):
    lang = lang_of(message.from_user)
    if message.chat.type not in GROUP_TYPES:
        await message.reply(t("cfg_group_only", lang))
        return
    if not await _is_admin(message.bot, message.chat.id, message.from_user.id):
        await message.reply(t("cfg_admin_only", lang))
        return
    async with SessionLocal() as session:
        disabled = await get_disabled_features(session, message.chat.id)
    await message.reply(t("cfg_title", lang), reply_markup=_keyboard(disabled, lang))


@router.callback_query(F.data.startswith("cfg:"))
async def toggle_feature(callback: CallbackQuery):
    lang = lang_of(callback.from_user)
    feature = callback.data.split(":", 1)[1]
    chat_id = callback.message.chat.id
    # Переключать могут только админы (проверяем именно нажавшего)
    if not await _is_admin(callback.bot, chat_id, callback.from_user.id):
        await callback.answer(t("cfg_admin_only", lang), show_alert=True)
        return
    async with SessionLocal() as session:
        disabled = await get_disabled_features(session, chat_id)
        # если сейчас выключена — включаем, и наоборот
        await set_feature(session, chat_id, feature, enable=(feature in disabled))
        disabled = await get_disabled_features(session, chat_id)
    await callback.message.edit_reply_markup(reply_markup=_keyboard(disabled, lang))
    await callback.answer()
