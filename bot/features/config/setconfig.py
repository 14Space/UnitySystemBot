"""
Команда /setconfig для @viaUnitySystem: администратор группы включает/выключает
функции бота в этой группе (транскрибация, скачивание, позже — конвертация валют).
Подключается только к боту-комбайну (with_config=True), обычных ботов не касается.
"""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from bot.database import SessionLocal
from bot.database.repository import (
    get_disabled_features, set_feature, get_slideshow_mode, set_slideshow_mode,
)
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


# Режимы слайдшоу: (внутреннее имя, ключ подписи). Порядок кнопок в ряду.
SLIDESHOW_MODES = [
    ("photos", "tt_as_photos"),
    ("video", "tt_as_video"),
    ("ask", "cfg_slideshow_ask"),
]


def _keyboard(disabled: set[str], ss_mode: str, lang: str) -> InlineKeyboardMarkup:
    rows = []
    # Переключатели функций (вкл/выкл)
    for feature, label_key in TOGGLEABLE:
        mark = "❌" if feature in disabled else "✅"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {t(label_key, lang)}", callback_data=f"cfg:{feature}")])
    # Заголовок блока слайдшоу (некликабельный — по нажатию просто ничего не делаем)
    rows.append([InlineKeyboardButton(
        text=t("cfg_slideshow_header", lang), callback_data="cfg:noop")])
    # Три режима в одном ряду, активный помечаем «радио-точкой»
    ss_row = []
    for mode, label_key in SLIDESHOW_MODES:
        mark = "✅ " if ss_mode == mode else ""
        ss_row.append(InlineKeyboardButton(
            text=f"{mark}{t(label_key, lang)}", callback_data=f"cfg:ss:{mode}"))
    rows.append(ss_row)
    # Кнопка «Готово» — сохранять не нужно (всё сохраняется сразу), она убирает
    # сообщение настроек, чтобы по нему потом случайно не тыкали.
    rows.append([InlineKeyboardButton(text=t("cfg_done", lang), callback_data="cfg:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        ss_mode = await get_slideshow_mode(session, message.chat.id)
    await message.reply(t("cfg_title", lang), reply_markup=_keyboard(disabled, ss_mode, lang))


async def _refresh(callback: CallbackQuery, lang: str):
    """Перерисовывает клавиатуру настроек актуальным состоянием."""
    chat_id = callback.message.chat.id
    async with SessionLocal() as session:
        disabled = await get_disabled_features(session, chat_id)
        ss_mode = await get_slideshow_mode(session, chat_id)
    await callback.message.edit_reply_markup(reply_markup=_keyboard(disabled, ss_mode, lang))


@router.callback_query(F.data == "cfg:noop")
async def cfg_noop(callback: CallbackQuery):
    # Заголовок-строка: просто гасим «часики», ничего не делаем
    await callback.answer()


@router.callback_query(F.data == "cfg:done")
async def cfg_done(callback: CallbackQuery):
    lang = lang_of(callback.from_user)
    chat_id = callback.message.chat.id
    if not await _is_admin(callback.bot, chat_id, callback.from_user.id):
        await callback.answer(t("cfg_admin_only", lang), show_alert=True)
        return
    # Настройки уже сохранены на каждом нажатии — просто убираем сообщение,
    # чтобы по нему потом случайно не тыкали.
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("cfg:ss:"))
async def set_slideshow(callback: CallbackQuery):
    lang = lang_of(callback.from_user)
    chat_id = callback.message.chat.id
    mode = callback.data.split(":", 2)[2]
    if not await _is_admin(callback.bot, chat_id, callback.from_user.id):
        await callback.answer(t("cfg_admin_only", lang), show_alert=True)
        return
    async with SessionLocal() as session:
        await set_slideshow_mode(session, chat_id, mode)
    await _refresh(callback, lang)
    await callback.answer()


@router.callback_query(F.data.in_({"cfg:transcribe", "cfg:download"}))
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
    await _refresh(callback, lang)
    await callback.answer()
