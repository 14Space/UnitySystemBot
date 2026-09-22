"""Оплата криптой через Crypto Pay (@CryptoBot).

Главное, что здесь проверяется, — деньги и премиум не расходятся: один оплаченный
счёт даёт ровно один премиум и ровно одну запись о покупке, сколько бы раз опрос
ни увидел этот счёт (перезапуск, повторный ответ сервиса, вторая проверка подряд).
"""
import asyncio
import importlib

import pytest

from bot.features.common import cryptopay


def test_disabled_without_token(monkeypatch):
    """Нет токена — способа оплаты нет вовсе: кнопка не должна вести в никуда."""
    monkeypatch.setattr(cryptopay, "CRYPTOPAY_TOKEN", "")
    assert cryptopay.available() is False


def test_buy_button_hides_crypto_when_not_configured(monkeypatch):
    from bot.features.common import payment

    monkeypatch.setattr(payment.cryptopay, "available", lambda: False)
    only_stars = payment.buy_button("ru")
    monkeypatch.setattr(payment.cryptopay, "available", lambda: True)
    with_crypto = payment.buy_button("ru")

    assert len(only_stars.inline_keyboard) == 1
    assert len(with_crypto.inline_keyboard) == 2
    assert with_crypto.inline_keyboard[1][0].callback_data == "buy_crypto"


def test_invoice_is_priced_in_dollars(monkeypatch):
    """Счёт выставляем в долларах: иначе цена плясала бы вместе с курсом монеты."""
    sent = {}

    def fake_api(method, **params):
        sent["method"], sent["params"] = method, params
        return {"invoice_id": 1, "bot_invoice_url": "https://t.me/CryptoBot?start=1"}

    monkeypatch.setattr(cryptopay, "_api", fake_api)
    monkeypatch.setattr(cryptopay, "PREMIUM_PRICE_USD", 3.5)
    cryptopay.create_invoice(42, "описание", "спасибо")

    assert sent["method"] == "createInvoice"
    assert sent["params"]["currency_type"] == "fiat"
    assert sent["params"]["fiat"] == "USD"
    assert sent["params"]["amount"] == "3.50"
    assert sent["params"]["payload"] == "42"       # по нему узнаём покупателя


def test_no_ids_means_no_request(monkeypatch):
    """Пустой список — не повод спрашивать: без фильтра вернулись бы ВСЕ счета."""
    def boom(method, **params):
        raise AssertionError("лишний запрос")

    monkeypatch.setattr(cryptopay, "_api", boom)
    assert cryptopay.get_invoices([]) == []


def test_answer_shapes_are_both_understood(monkeypatch):
    """API отдаёт то массив, то объект со списком — понимаем оба вида."""
    monkeypatch.setattr(cryptopay, "_api", lambda m, **p: [{"invoice_id": 1}])
    assert cryptopay.get_invoices([1]) == [{"invoice_id": 1}]
    monkeypatch.setattr(cryptopay, "_api", lambda m, **p: {"items": [{"invoice_id": 2}]})
    assert cryptopay.get_invoices([2]) == [{"invoice_id": 2}]


def test_refusal_is_not_silent(monkeypatch):
    """«200 OK» у Crypto Pay ничего не значит — смотреть надо на поле ok."""
    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": False, "error": {"code": 400, "name": "AUTHORIZATION_INVALID"}}

    monkeypatch.setattr(cryptopay.requests, "post", lambda *a, **kw: Resp())
    monkeypatch.setattr(cryptopay, "CRYPTOPAY_TOKEN", "токен")
    with pytest.raises(RuntimeError):
        cryptopay.get_me()


def test_paid_invoice_grants_premium_once(tmp_path, monkeypatch):
    """Дважды увидели оплату — премиум и запись о покупке всё равно по одному разу."""
    import bot.config as config
    import bot.database as db

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    db = importlib.reload(db)

    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text):
            sent.append(chat_id)

    async def scenario():
        await db.init_db()
        import bot.database.repository as repo
        import bot.features.common.payment as payment
        importlib.reload(repo)
        monkeypatch.setattr(payment, "SessionLocal", db.SessionLocal)
        monkeypatch.setattr(payment, "set_premium", repo.set_premium)
        monkeypatch.setattr(payment, "add_payment", repo.add_payment)
        monkeypatch.setattr(payment, "user_language", repo.user_language)

        async with db.SessionLocal() as s:
            await repo.get_or_create_user(s, 77, "кто-то")

        await payment.grant_crypto(FakeBot(), 77, 555, 3.5)
        await payment.grant_crypto(FakeBot(), 77, 555, 3.5)   # повтор опроса

        async with db.SessionLocal() as s:
            summary = await repo.get_payment_summary(s)
            premium = await repo.is_premium(s, 77)
        await db.engine.dispose()
        return summary, premium

    summary, premium = asyncio.run(scenario())
    assert premium is True
    assert summary["count"] == 1              # одна покупка, а не две
    assert summary["usd"] == pytest.approx(3.5)
    assert len(sent) == 2                     # человеку сказали оба раза — это не страшно


def test_open_invoices_are_tracked(tmp_path, monkeypatch):
    """Счёт живёт в базе, пока не оплачен: иначе перезапуск бота потеряет покупку."""
    import bot.config as config
    import bot.database as db

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'i.db'}")
    db = importlib.reload(db)

    async def scenario():
        await db.init_db()
        import bot.database.repository as repo
        importlib.reload(repo)
        async with db.SessionLocal() as s:
            await repo.add_crypto_invoice(s, 10, 1)
            await repo.add_crypto_invoice(s, 11, 2)
            await repo.add_crypto_invoice(s, 10, 1)       # повтор — не дубль
            before = await repo.open_crypto_invoices(s)
            await repo.close_crypto_invoice(s, 10, "paid")
            after = await repo.open_crypto_invoices(s)
        await db.engine.dispose()
        return before, after

    before, after = asyncio.run(scenario())
    assert sorted(before) == [(10, 1), (11, 2)]
    assert after == [(11, 2)]
