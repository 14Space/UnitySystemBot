"""База данных: настройки файла и новые таблицы.

Проверяем на временном файле — настоящую базу бота тесты не трогают.
"""
import asyncio

import pytest


def test_wal_and_timeout_are_applied(tmp_path, monkeypatch):
    """Без WAL параллельные записи дают «database is locked»."""
    import importlib
    import bot.config as config
    import bot.database as db

    path = tmp_path / "probe.db"
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    db = importlib.reload(db)

    async def scenario():
        await db.init_db()
        async with db.engine.connect() as conn:
            mode = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
            sync = (await conn.exec_driver_sql("PRAGMA synchronous")).scalar()
        await db.engine.dispose()
        return mode, sync

    mode, sync = asyncio.run(scenario())
    assert mode.lower() == "wal"
    assert sync == 1            # NORMAL


def test_payments_table_is_created(tmp_path, monkeypatch):
    import importlib
    import bot.config as config
    import bot.database as db

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
    db = importlib.reload(db)

    async def scenario():
        await db.init_db()
        from bot.database.repository import add_payment, refund_payment, get_payment_summary
        async with db.SessionLocal() as s:
            await add_payment(s, 1, "charge_1", 250)
            await add_payment(s, 1, "charge_1", 250)     # повтор от Telegram — не дубль
            await add_payment(s, 2, "charge_2", 250)
            first = await get_payment_summary(s)
            owner = await refund_payment(s, "charge_2")
            after = await get_payment_summary(s)
            missing = await refund_payment(s, "нет такого")
        await db.engine.dispose()
        return first, owner, after, missing

    first, owner, after, missing = asyncio.run(scenario())
    assert first == {"count": 2, "stars": 500, "refunded": 0}
    assert owner == 2
    assert after == {"count": 1, "stars": 250, "refunded": 1}
    assert missing is None
