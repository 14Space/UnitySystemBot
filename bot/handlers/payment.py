from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from bot.config import PREMIUM_PRICE_STARS
from bot.database import SessionLocal
from bot.database.repository import is_premium, set_premium

router = Router()

PREMIUM_DESC = "С Premium ⭐ видео можно скачивать в самом высоком разрешении, а музыку – целыми альбомами."

PREMIUM_TEXT = (
    "✨ <b>viaSaver Premium</b>\n\n"
    f"{PREMIUM_DESC}\n\n"
    f"Разовая покупка, навсегда. Цена: <b>{PREMIUM_PRICE_STARS} ⭐</b>"
)


def buy_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"Купить за {PREMIUM_PRICE_STARS} ⭐", callback_data="buy_premium")
    ]])


async def _send_invoice(bot: Bot, chat_id: int):
    await bot.send_invoice(
        chat_id,
        title="viaSaver Premium",
        description=PREMIUM_DESC,
        payload="premium",
        currency="XTR",  # Telegram Stars
        prices=[LabeledPrice(label="Premium", amount=PREMIUM_PRICE_STARS)],
    )


@router.message(Command("premium"))
async def cmd_premium(message: Message):
    async with SessionLocal() as session:
        if await is_premium(session, message.from_user.id):
            await message.answer("✨ У тебя уже есть Premium — все функции открыты!")
            return
    await message.answer(PREMIUM_TEXT, parse_mode="HTML", reply_markup=buy_button())


@router.callback_query(F.data == "buy_premium")
async def cb_buy_premium(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    async with SessionLocal() as session:
        if await is_premium(session, callback.from_user.id):
            await callback.message.answer("✨ У тебя уже есть Premium!")
            return
    await _send_invoice(bot, callback.message.chat.id)


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, bot: Bot):
    # Подтверждаем готовность принять платёж (обязательный шаг)
    await bot.answer_pre_checkout_query(query.id, ok=True)


@router.message(F.successful_payment)
async def on_success(message: Message):
    async with SessionLocal() as session:
        await set_premium(session, message.from_user.id, True)
    await message.answer("✨ Спасибо за покупку! Premium активирован 🎉\nВсе функции открыты.")
