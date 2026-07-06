from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from bot.config import PREMIUM_PRICE_STARS
from bot.database import SessionLocal
from bot.database.repository import is_premium, set_premium
from bot.utils.i18n import t, lang_of

router = Router()


def buy_button(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=t("btn_buy", lang, price=PREMIUM_PRICE_STARS), callback_data="buy_premium"
        )
    ]])


async def _send_invoice(bot: Bot, chat_id: int, lang: str):
    await bot.send_invoice(
        chat_id,
        title="viaSaver Premium",
        description=t("premium_desc", lang),
        payload="premium",
        currency="XTR",  # Telegram Stars
        prices=[LabeledPrice(label="Premium", amount=PREMIUM_PRICE_STARS)],
    )


@router.message(Command("premium"))
async def cmd_premium(message: Message):
    lang = lang_of(message.from_user)
    async with SessionLocal() as session:
        if await is_premium(session, message.from_user.id):
            await message.answer(t("already_premium", lang))
            return
    text = t("premium_text", lang, desc=t("premium_desc", lang), price=PREMIUM_PRICE_STARS)
    await message.answer(text, parse_mode="HTML", reply_markup=buy_button(lang))


@router.callback_query(F.data == "buy_premium")
async def cb_buy_premium(callback: CallbackQuery, bot: Bot):
    lang = lang_of(callback.from_user)
    await callback.answer()
    async with SessionLocal() as session:
        if await is_premium(session, callback.from_user.id):
            await callback.message.answer(t("already_premium_short", lang))
            return
    await _send_invoice(bot, callback.message.chat.id, lang)


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, bot: Bot):
    # Подтверждаем готовность принять платёж (обязательный шаг)
    await bot.answer_pre_checkout_query(query.id, ok=True)


@router.message(F.successful_payment)
async def on_success(message: Message):
    async with SessionLocal() as session:
        await set_premium(session, message.from_user.id, True)
    await message.answer(t("payment_success", lang_of(message.from_user)))
