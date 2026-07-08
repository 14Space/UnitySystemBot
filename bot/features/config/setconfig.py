"""
Команда /setconfig: администратор группы включает/выключает функции бота в этой
группе. Доступна ботам с config=True (viaUnity и viaSaver). Клавиатура подстраивается
под функции конкретного бота: у viaSaver (только скачивание) не будет разделов
транскрибации и конвертации валют — см. _keyboard(features=...).
"""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from bot.database import SessionLocal
from bot.database.repository import (
    get_disabled_features, set_feature, get_slideshow_mode, set_slideshow_mode,
    get_currency_targets, toggle_currency_target, get_audio_track, set_audio_track,
)
from bot.features.currency.parser import ORDER as CURRENCY_ORDER
from bot.utils.i18n import t, lang_of

router = Router()

GROUP_TYPES = ("group", "supergroup")

# Функции-переключатели отдельной строкой: (внутреннее имя, ключ подписи).
# Конвертер сюда НЕ входит — им управляет заголовок блока валют (см. _keyboard).
TOGGLEABLE = [
    ("transcribe", "cfg_transcribe"),
    ("download", "cfg_download"),
]

# Множество callback'ов переключателей функций (для точного матча хендлера).
# Конвертер (cfg:currency) обрабатывается тем же toggle_feature, добавляем явно.
_FEATURE_CB = {f"cfg:{name}" for name, _ in TOGGLEABLE} | {"cfg:currency"}


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


def _keyboard(disabled: set[str], ss_mode: str, targets: list[str], audio_on: bool,
              features: set[str], lang: str) -> InlineKeyboardMarkup:
    """Клавиатура настроек. Показываем только разделы под функции конкретного бота:
    у viaSaver (только скачивание) не будет транскрибации и конвертера валют."""
    rows = []
    # Переключатели функций — только те, что у этого бота есть
    for feature, label_key in TOGGLEABLE:
        if feature not in features:
            continue
        mark = "❌" if feature in disabled else "✅"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {t(label_key, lang)}", callback_data=f"cfg:{feature}")])

    # Аудиодорожка и слайдшоу относятся к скачиванию
    if "download" in features:
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if audio_on else '❌'} {t('cfg_audio_track', lang)}",
            callback_data="cfg:audio")])
        rows.append([InlineKeyboardButton(
            text=t("cfg_slideshow_header", lang), callback_data="cfg:noop")])
        ss_row = []
        for mode, label_key in SLIDESHOW_MODES:
            mark = "✅ " if ss_mode == mode else ""
            ss_row.append(InlineKeyboardButton(
                text=f"{mark}{t(label_key, lang)}", callback_data=f"cfg:ss:{mode}"))
        rows.append(ss_row)

    # Блок валют — только если бот умеет конвертацию. Заголовок = переключатель
    # конвертера: ✅ включён (показываем сетку), ❌ выключен (сетку прячем).
    if "currency" in features:
        cur_on = "currency" not in disabled
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if cur_on else '❌'} {t('cfg_currency_header', lang)}",
            callback_data="cfg:currency")])
        if cur_on:
            row = []
            for code in CURRENCY_ORDER:
                mark = "🔘 " if code in targets else ""
                row.append(InlineKeyboardButton(text=f"{mark}{code}", callback_data=f"cfg:cur:{code}"))
                if len(row) == 3:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

    # Кнопка «Готово» — убирает сообщение настроек, чтобы по нему потом не тыкали.
    rows.append([InlineKeyboardButton(text=t("cfg_done", lang), callback_data="cfg:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("setconfig"))
async def cmd_setconfig(message: Message, bot_features: set[str] = frozenset()):
    lang = lang_of(message.from_user)
    if message.chat.type not in GROUP_TYPES:
        await message.reply(t("cfg_group_only", lang))
        return
    # Не-админам не отвечаем вообще — команда для них будто не существует
    # (чтобы бот не реагировал на /setconfig от любого участника группы).
    if not await _is_admin(message.bot, message.chat.id, message.from_user.id):
        return
    async with SessionLocal() as session:
        disabled = await get_disabled_features(session, message.chat.id)
        ss_mode = await get_slideshow_mode(session, message.chat.id)
        targets = await get_currency_targets(session, message.chat.id)
        audio_on = await get_audio_track(session, message.chat.id)
    await message.reply(t("cfg_title", lang),
                        reply_markup=_keyboard(disabled, ss_mode, targets, audio_on, bot_features, lang))


async def _refresh(callback: CallbackQuery, lang: str, features: set[str]):
    """Перерисовывает клавиатуру настроек актуальным состоянием."""
    chat_id = callback.message.chat.id
    async with SessionLocal() as session:
        disabled = await get_disabled_features(session, chat_id)
        ss_mode = await get_slideshow_mode(session, chat_id)
        targets = await get_currency_targets(session, chat_id)
        audio_on = await get_audio_track(session, chat_id)
    await callback.message.edit_reply_markup(
        reply_markup=_keyboard(disabled, ss_mode, targets, audio_on, features, lang))


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
async def set_slideshow(callback: CallbackQuery, bot_features: set[str] = frozenset()):
    lang = lang_of(callback.from_user)
    chat_id = callback.message.chat.id
    mode = callback.data.split(":", 2)[2]
    if not await _is_admin(callback.bot, chat_id, callback.from_user.id):
        await callback.answer(t("cfg_admin_only", lang), show_alert=True)
        return
    async with SessionLocal() as session:
        await set_slideshow_mode(session, chat_id, mode)
    await _refresh(callback, lang, bot_features)
    await callback.answer()


@router.callback_query(F.data == "cfg:audio")
async def toggle_audio(callback: CallbackQuery, bot_features: set[str] = frozenset()):
    lang = lang_of(callback.from_user)
    chat_id = callback.message.chat.id
    if not await _is_admin(callback.bot, chat_id, callback.from_user.id):
        await callback.answer(t("cfg_admin_only", lang), show_alert=True)
        return
    async with SessionLocal() as session:
        now = await get_audio_track(session, chat_id)
        await set_audio_track(session, chat_id, not now)
    await _refresh(callback, lang, bot_features)
    await callback.answer()


@router.callback_query(F.data.startswith("cfg:cur:"))
async def toggle_currency(callback: CallbackQuery, bot_features: set[str] = frozenset()):
    lang = lang_of(callback.from_user)
    chat_id = callback.message.chat.id
    code = callback.data.split(":", 2)[2]
    if not await _is_admin(callback.bot, chat_id, callback.from_user.id):
        await callback.answer(t("cfg_admin_only", lang), show_alert=True)
        return
    async with SessionLocal() as session:
        await toggle_currency_target(session, chat_id, code)
    await _refresh(callback, lang, bot_features)
    await callback.answer()


@router.callback_query(F.data.in_(_FEATURE_CB))
async def toggle_feature(callback: CallbackQuery, bot_features: set[str] = frozenset()):
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
    await _refresh(callback, lang, bot_features)
    await callback.answer()
