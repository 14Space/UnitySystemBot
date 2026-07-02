from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.utils.i18n import t


def build_tiktok_slideshow_keyboard(sid: str, lang: str) -> InlineKeyboardMarkup:
    """Кнопки выбора формата слайдшоу TikTok: видео (со звуком) или отдельные фото."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("tt_as_video", lang), callback_data=f"ttdl:video:{sid}")
    builder.button(text=t("tt_as_photos", lang), callback_data=f"ttdl:photos:{sid}")
    builder.adjust(2)
    return builder.as_markup()
