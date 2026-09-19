import logging

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from bot.config import PREMIUM_PRICE_STARS
from bot.database import SessionLocal
from bot.database.repository import is_premium, set_premium, add_payment, refund_payment
from bot.utils.i18n import t, lang_of

logger = logging.getLogger(__name__)

router = Router()

# Что написано в счёте. Telegram возвращает это же значение в подтверждении оплаты —
# по нему мы узнаём СВОЙ счёт: в чате может лежать старый счёт или счёт другого бота.
PAYLOAD = "premium"


def buy_button(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=t("btn_buy", lang, price=PREMIUM_PRICE_STARS), callback_data="buy_premium"
        )
    ]])


async def _send_invoice(bot: Bot, chat_id: int, lang: str):
    await bot.send_invoice(
        chat_id,
        title="UnitySystem Premium",
        description=t("premium_desc", lang),
        payload=PAYLOAD,
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
    """Последний шаг перед списанием звёзд: Telegram спрашивает, берём ли мы этот
    платёж. Отвечаем «да» только на свой счёт — чужой или устаревший отклоняем, иначе
    списание пройдёт, а товар выдавать не за что."""
    if query.invoice_payload != PAYLOAD:
        await bot.answer_pre_checkout_query(
            query.id, ok=False,
            error_message=t("payment_stale", lang_of(query.from_user)))
        logger.warning("Отклонён платёж с чужим счётом: %r", query.invoice_payload)
        return
    await bot.answer_pre_checkout_query(query.id, ok=True)


@router.message(F.successful_payment)
async def on_success(message: Message):
    """Звёзды списаны. Кроме выдачи премиума ОБЯЗАТЕЛЬНО записываем номер платежа
    (telegram_payment_charge_id): без него возврат звёзд невозможен в принципе."""
    pay = message.successful_payment
    async with SessionLocal() as session:
        await set_premium(session, message.from_user.id, True)
        await add_payment(session, message.from_user.id,
                          pay.telegram_payment_charge_id, pay.total_amount or 0)
    logger.info("Оплата премиума: user=%s stars=%s charge=%s",
                message.from_user.id, pay.total_amount, pay.telegram_payment_charge_id)
    await message.answer(t("payment_success", lang_of(message.from_user)))


@router.message(F.refunded_payment)
async def on_refund(message: Message):
    """Звёзды вернули (через поддержку Telegram или командой возврата). Премиум надо
    снять: иначе человек получает его навсегда бесплатно, и узнать об этом неоткуда —
    Telegram нам ничего больше не напомнит."""
    charge_id = message.refunded_payment.telegram_payment_charge_id
    async with SessionLocal() as session:
        user_id = await refund_payment(session, charge_id) or message.from_user.id
        await set_premium(session, user_id, False)
    logger.info("Возврат звёзд: user=%s charge=%s", user_id, charge_id)
    await message.answer(t("payment_refunded", lang_of(message.from_user)))
