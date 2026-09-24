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
    assert first == {"count": 2, "stars": 500, "usd": 0.0, "refunded": 0}
    assert owner == 2
    assert after == {"count": 1, "stars": 250, "usd": 0.0, "refunded": 1}
    assert missing is None


def test_old_database_gets_new_columns(tmp_path, monkeypatch):
    """Базу старой версии дописываем колонками при старте. Раньше список колонок
    читался через PRAGMA – это есть только у SQLite; теперь через SQLAlchemy, и
    проверяем, что для SQLite от этого ничего не сломалось."""
    import importlib
    import sqlite3
    import bot.config as config
    import bot.database as db

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE chat_settings (id INTEGER PRIMARY KEY, chat_id INTEGER, "
                "disabled_features VARCHAR)")
    con.execute("INSERT INTO chat_settings (chat_id, disabled_features) VALUES (5, '')")
    con.commit()
    con.close()

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    db = importlib.reload(db)

    async def scenario():
        await db.init_db()
        await db.init_db()          # повторный старт ничего не ломает
        await db.engine.dispose()

    asyncio.run(scenario())
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(chat_settings)")}
    row = con.execute("SELECT slideshow_mode, audio_track, compress_shorts "
                      "FROM chat_settings WHERE chat_id = 5").fetchone()
    con.close()
    assert {"slideshow_mode", "currency_targets", "audio_track", "compress_shorts"} <= cols
    assert row == ("video", 0, None)


def test_chat_settings_are_cached_and_dropped_on_change(tmp_path, monkeypatch):
    """Настройки чата читаются из базы раз в минуту, а не на каждую ссылку, – но любое
    изменение через /setconfig видно сразу."""
    import importlib
    import bot.config as config
    import bot.database as db

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cs.db'}")
    db = importlib.reload(db)
    from bot.database import repository as r

    async def scenario():
        await db.init_db()
        async with db.SessionLocal() as s:
            assert await r.get_slideshow_mode(s, 7, default="ask") == "ask"
            await r.set_slideshow_mode(s, 7, "photos")
            assert await r.get_slideshow_mode(s, 7) == "photos"     # сразу, без минуты
            await r.set_compress_shorts(s, 7, True)
            assert await r.get_compress_shorts(s, 7, default=False) is True
            await r.toggle_currency_target(s, 7, "UAH")
            assert "UAH" in await r.get_currency_targets(s, 7)
            await r.set_feature(s, 7, "ai", enable=False)
            assert await r.get_disabled_features(s, 7) == {"ai"}

        # Второй раз в течение минуты – из памяти: базу даже не спрашиваем.
        calls = []
        async with db.SessionLocal() as s:
            real = s.execute

            async def counting(*a, **kw):
                calls.append(1)
                return await real(*a, **kw)
            s.execute = counting
            assert await r.get_slideshow_mode(s, 7) == "photos"
            assert await r.get_audio_track(s, 7) is True
        await db.engine.dispose()
        return calls

    assert asyncio.run(scenario()) == []
