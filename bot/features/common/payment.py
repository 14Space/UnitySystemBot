import asyncio
import logging

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from bot.config import PREMIUM_PRICE_STARS, PREMIUM_PRICE_USD
from bot.database import SessionLocal
from bot.database.repository import (
    is_premium, set_premium, add_payment, refund_payment, add_crypto_invoice,
    user_language,
)
from bot.features.common import cryptopay
from bot.utils.i18n import t, lang_of

logger = logging.getLogger(__name__)

router = Router()

# Что написано в счёте. Telegram возвращает это же значение в подтверждении оплаты —
# по нему мы узнаём СВОЙ счёт: в чате может лежать старый счёт или счёт другого бота.
PAYLOAD = "premium"


# Как помечаем крипто-покупку в таблице платежей. Номер счёта уникален у Crypto Pay,
# но не у Telegram — приставка разводит их между собой и заодно видно происхождение.
CRYPTO_CHARGE = "cryptopay:{id}"


def pay_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Кнопки под счётом: оплата звёздами и, если настроена, крипта.

    Первая кнопка ОБЯЗАНА быть кнопкой оплаты (pay=True) — таково правило Telegram
    для счетов. Раньше своей клавиатуры не было вовсе, и Telegram рисовал её сам;
    теперь рисуем мы, чтобы рядом со звёздами стояла крипта.

    Крипта появляется только когда она настроена: показывать способ оплаты, который
    не работает, — худшее, что можно сделать.
    """
    rows = [[InlineKeyboardButton(
        text=t("btn_pay_stars", lang, price=PREMIUM_PRICE_STARS), pay=True)]]
    if cryptopay.available():
        rows.append([InlineKeyboardButton(
            text=t("btn_buy_crypto", lang), callback_data="buy_crypto")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_invoice(bot: Bot, chat_id: int, lang: str):
    """Счёт на звёзды. Он же — единственный экран покупки: раньше перед ним висело
    отдельное сообщение с кнопкой «Купить», и человек делал лишнее нажатие ни за чем.

    Описание уходит в Telegram простым текстом: разметку в счетах он не принимает,
    поэтому выделить в нём слово нельзя — жирным будет только заголовок.
    """
    await bot.send_invoice(
        chat_id,
        # Заголовок Telegram рисует жирным сам, а вот значок надо ставить руками:
        # разметки в счетах нет, и это единственный способ его оживить.
        title="✨ UnitySystem Premium",
        description=t("premium_desc", lang),
        payload=PAYLOAD,
        currency="XTR",  # Telegram Stars
        prices=[LabeledPrice(label="Premium", amount=PREMIUM_PRICE_STARS)],
        reply_markup=pay_keyboard(lang),
    )


@router.message(Command("premium"))
async def cmd_premium(message: Message, bot: Bot):
    lang = lang_of(message.from_user)
    async with SessionLocal() as session:
        if await is_premium(session, message.from_user.id):
            await message.answer(t("already_premium", lang))
            return
    await _send_invoice(bot, message.chat.id, lang)


@router.callback_query(F.data == "buy_premium")
async def cb_buy_premium(callback: CallbackQuery, bot: Bot):
    lang = lang_of(callback.from_user)
    await callback.answer()
    async with SessionLocal() as session:
        if await is_premium(session, callback.from_user.id):
            await callback.message.answer(t("already_premium_short", lang))
            return
    await _send_invoice(bot, callback.message.chat.id, lang)


@router.callback_query(F.data == "buy_crypto")
async def cb_buy_crypto(callback: CallbackQuery):
    """Выставляет счёт в Crypto Pay и даёт ссылку на оплату.

    Премиум здесь НЕ выдаём: оплату увидит фоновая задача (см. _watch_crypto_invoices
    в main.py). Так человек получит своё, даже если закроет бота сразу после оплаты
    или мы в этот момент перезапускались.
    """
    lang = lang_of(callback.from_user)
    await callback.answer()
    async with SessionLocal() as session:
        if await is_premium(session, callback.from_user.id):
            await callback.message.answer(t("already_premium_short", lang))
            return
    try:
        invoice = await asyncio.to_thread(
            cryptopay.create_invoice, callback.from_user.id,
            t("premium_desc", lang), t("crypto_paid_hint", lang))
    except Exception:
        logger.exception("Не вышло выставить крипто-счёт")
        await callback.message.answer(t("crypto_unavailable", lang))
        return
    async with SessionLocal() as session:
        await add_crypto_invoice(session, invoice["invoice_id"], callback.from_user.id)
    url = invoice.get("bot_invoice_url") or invoice.get("pay_url")
    await callback.message.answer(
        t("crypto_invoice", lang, price=f"{PREMIUM_PRICE_USD:.2f}"),
        parse_mode="HTML",           # без этого теги <b> уезжали человеку как текст
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=t("btn_pay_crypto", lang), url=url)]]))
    logger.info("Крипто-счёт выставлен: user=%s invoice=%s",
                callback.from_user.id, invoice["invoice_id"])


async def grant_crypto(bot: Bot, user_id: int, invoice_id: int, usd: float) -> None:
    """Счёт оплачен: включаем премиум и говорим об этом человеку.

    Запись платежа идёт по уникальному номеру счёта, а повторную запись репозиторий
    молча пропускает — так что второй заход опроса (или перезапуск в неудачный
    момент) не выдаст премиум дважды и не задвоит покупку в отчёте.
    """
    async with SessionLocal() as session:
        await set_premium(session, user_id, True)
        await add_payment(session, user_id, CRYPTO_CHARGE.format(id=invoice_id),
                          stars=0, method="crypto", usd=usd)
        lang = await user_language(session, user_id)
    logger.info("Оплата премиума криптой: user=%s invoice=%s usd=%s",
                user_id, invoice_id, usd)
    try:
        await bot.send_message(user_id, t("payment_success", lang))
    except Exception:
        # Человек мог закрыть чат с ботом. Премиум уже выдан — это важнее письма.
        logger.warning("Не смог сообщить об оплате user=%s", user_id)


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
