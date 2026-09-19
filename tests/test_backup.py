"""Резервная копия базы.

Смысл теста — не «файл создался», а «файл читается как база и данные в нём те же».
Копия, которую нельзя открыть, хуже отсутствия копии: о ней не знаешь, что она битая.
"""
import asyncio
import importlib
import os
import sqlite3


def test_backup_is_a_readable_database_with_the_same_rows(tmp_path, monkeypatch):
    import bot.config as config
    import bot.database as db

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'live.db'}")
    db = importlib.reload(db)
    backup = importlib.reload(importlib.import_module("bot.features.common.backup"))
    monkeypatch.setattr(backup, "tempfile", __import__("tempfile"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    async def scenario():
        await db.init_db()
        from bot.database.repository import add_payment
        async with db.SessionLocal() as s:
            await add_payment(s, 777, "charge_backup", 250)
        path = await backup.dump_database()
        await db.engine.dispose()
        return path

    path = asyncio.run(scenario())
    assert path and os.path.exists(path)
    try:
        con = sqlite3.connect(path)
        rows = con.execute("SELECT user_id, stars FROM payments").fetchall()
        con.close()
        assert rows == [(777, 250)]
    finally:
        os.remove(path)


def test_backup_overwrites_an_existing_file_for_the_same_day(tmp_path, monkeypatch):
    """Второй запуск в тот же день не должен падать из-за уже лежащего файла."""
    import bot.config as config
    import bot.database as db

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'live2.db'}")
    db = importlib.reload(db)
    backup = importlib.reload(importlib.import_module("bot.features.common.backup"))

    async def scenario():
        await db.init_db()
        first = await backup.dump_database()
        second = await backup.dump_database()
        await db.engine.dispose()
        return first, second

    first, second = asyncio.run(scenario())
    assert first == second and os.path.exists(second)
    os.remove(second)
