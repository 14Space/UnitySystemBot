"""
Приём оплаты криптой через Crypto Pay (@CryptoBot) — второй способ купить Premium.

Почему именно так, а не свой кошелёк с наблюдением за блокчейном. Оплата счёта идёт
ВНУТРИ CryptoBot, между кошельками: блокчейна на этом шаге нет, значит нет ни выбора
сети (человек не потеряет деньги, отправив USDT не туда), ни сетевой комиссии на
каждую покупку. И привязывать платёж к человеку по комментарию не нужно — счёт сам
несёт наше поле payload и возвращает его обратно. Комментарий был бы худшим из
вариантов: биржи их срезают, и платёж пришёл бы ничей.

Счёт выставляем в ДОЛЛАРАХ (currency_type=fiat), а платит человек тем, чем удобно —
USDT, TON и так далее. Иначе цена в крипте плясала бы вместе с курсом.

Об оплате узнаём ОПРОСОМ (см. _watch_crypto_invoices в main.py), а не вебхуком:
вебхук требует публичного HTTPS-адреса с сертификатом, а бот живёт на опросе Telegram
и домена у него нет. Опрос раз в 15 секунд человеку незаметен.

Токен приложения выдаёт сам @CryptoBot: Crypto Pay → Create App. Для проверок есть
тестовая сеть — тот же код с CRYPTOPAY_TESTNET=true и токеном @CryptoTestnetBot.
"""
import logging

from bot.utils import net

from bot.config import (
    CRYPTOPAY_TOKEN, CRYPTOPAY_TESTNET, CRYPTOPAY_ASSETS,
    CRYPTOPAY_INVOICE_TTL, PREMIUM_PRICE_USD,
)

logger = logging.getLogger(__name__)

_MAINNET = "https://pay.crypt.bot/api/"
_TESTNET = "https://testnet-pay.crypt.bot/api/"
# Дольше ждать незачем: запрос лёгкий, а висящий вызов задержит либо ответ человеку,
# либо весь цикл опроса.
_TIMEOUT = 15


def available() -> bool:
    """Настроена ли крипто-оплата. Без токена способ просто не показываем."""
    return bool(CRYPTOPAY_TOKEN)


def _api(method: str, **params) -> dict | list:
    """Зовёт метод Crypto Pay. Бросает RuntimeError, если сервис ответил отказом.

    Ответ всегда завёрнут в {"ok": ..., "result"/"error": ...}, поэтому «200 OK» сам
    по себе ничего не значит — смотреть надо на поле ok.
    """
    base = _TESTNET if CRYPTOPAY_TESTNET else _MAINNET
    # Через общую сессию: опрос идёт каждые 15 секунд, и без неё каждый запрос заново
    # договаривался бы о шифровании (лишний круг до сервера и обратно).
    r = net.session().post(base + method, json=params,
                           headers={"Crypto-Pay-API-Token": CRYPTOPAY_TOKEN},
                           timeout=_TIMEOUT)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"Crypto Pay ответил не по-человечески ({r.status_code})")
    if not data.get("ok"):
        raise RuntimeError(f"Crypto Pay: {data.get('error')}")
    return data.get("result")


def _items(result) -> list:
    """Список счетов из ответа. API отдаёт то массив, то объект со списком внутри —
    поддерживаем оба вида, чтобы смена формата не оставила людей без премиума."""
    if isinstance(result, dict):
        return result.get("items") or []
    return result or []


def get_me() -> dict:
    """Проверка токена — ею же живёт пункт проверки функционала."""
    return _api("getMe")


def create_invoice(user_id: int, description: str, hidden_message: str) -> dict:
    """Выставляет счёт на Premium. Возвращает счёт целиком (нужны invoice_id и ссылка).

    В payload кладём id покупателя: сервис вернёт его вместе с оплатой, и мы точно
    знаем, кому включать премиум, даже если человек заплатил с другого устройства.
    """
    return _api(
        "createInvoice",
        currency_type="fiat",
        fiat="USD",
        amount=f"{PREMIUM_PRICE_USD:.2f}",
        accepted_assets=CRYPTOPAY_ASSETS,
        description=description[:1024],
        hidden_message=hidden_message[:2048],
        payload=str(user_id),
        expires_in=CRYPTOPAY_INVOICE_TTL,
        allow_anonymous=False,
    )


def get_invoices(invoice_ids: list[int]) -> list[dict]:
    """Состояние наших счетов. Пустой список на входе — пустой на выходе: без этого
    запрос без фильтра вернул бы ВСЕ счета приложения."""
    if not invoice_ids:
        return []
    return _items(_api("getInvoices",
                       invoice_ids=",".join(str(i) for i in invoice_ids)))
