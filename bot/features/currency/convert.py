"""
Конвертер валют. Работает только в группах (в личке отключён на уровне маршрутизатора).
Ловит «число+валюта» в сообщении и отвечает переводом в валюты, выбранные для этой
группы в /setconfig. Формат ответа:

    100$ это:
    95 евро
    25 шекелей
"""
import logging

from aiogram import Router
from aiogram.filters import BaseFilter
from aiogram.types import Message

from bot.database import SessionLocal
from bot.database.repository import get_currency_targets
from bot.features.currency import rates
from bot.features.currency.parser import parse, fmt_amount, CURRENCIES
from bot.utils.i18n import t, lang_of

router = Router()
logger = logging.getLogger(__name__)


class CurrencyText(BaseFilter):
    """Пропускает только сообщения, где реально распознан запрос «число+валюта».
    Иначе конвертер не срабатывает и сообщение уходит дальше (напр. в скачивание)."""
    async def __call__(self, message: Message) -> bool | dict:
        if not message.text:
            return False
        parsed = parse(message.text)
        if not parsed:
            return False
        return {"parsed": parsed}


def _source_label(amount: float, code: str) -> str:
    """Исходная сумма с флагом: «🇬🇧4 200£» или «🇨🇳100 CNY», если символа нет."""
    cur = CURRENCIES[code]
    body = f"{fmt_amount(amount)}{cur['symbol']}" if cur["symbol"] else f"{fmt_amount(amount)} {code}"
    return f"{cur['flag']}{body}"


@router.message(CurrencyText())
async def handle_currency(message: Message, parsed: tuple[float, str]):
    amount, code = parsed
    async with SessionLocal() as session:
        targets = await get_currency_targets(session, message.chat.id)
    # Не переводим валюту саму в себя
    targets = [c for c in targets if c != code]
    if not targets:
        return

    result = await rates.convert(amount, code, targets)
    if not result:
        return

    lang = lang_of(message.from_user)
    # Заголовок с исходной суммой, затем пустая строка, затем переводы (каждый с флагом)
    lines = [t("cur_head", lang, src=_source_label(amount, code)), ""]
    for c in targets:                       # сохраняем порядок из настроек
        val = result.get(c)
        if val is None:
            continue
        cur = CURRENCIES[c]
        name = cur["names"].get(lang) or cur["names"]["en"]
        lines.append(f"{cur['flag']}{fmt_amount(val)} {name}")

    if len(lines) > 2:                       # есть хотя бы один перевод
        await message.reply("\n".join(lines))
