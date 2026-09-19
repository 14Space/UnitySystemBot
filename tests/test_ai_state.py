"""Память ИИ: ветки разговора и суточный лимит переживают перезапуск бота."""
import asyncio
import importlib
from datetime import datetime, timezone


def _fresh_db(tmp_path, monkeypatch, name):
    import bot.config as config
    import bot.database as db

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / name}")
    return importlib.reload(db)


def test_thread_is_restored_after_restart(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch, "ai1.db")

    async def scenario():
        await db.init_db()
        from bot.database.repository import save_ai_thread, load_ai_thread
        history = [{"role": "user", "content": "привет"},
                   {"role": "assistant", "content": "здравствуй"}]
        async with db.SessionLocal() as s:
            await save_ai_thread(s, 555, -100, history)
        # «перезапуск»: новая сессия, ничего в памяти
        async with db.SessionLocal() as s:
            found = await load_ai_thread(s, 555)
            missing = await load_ai_thread(s, 999)
        await db.engine.dispose()
        return found, missing, history

    found, missing, history = asyncio.run(scenario())
    assert found == history
    assert missing is None


def test_thread_is_overwritten_not_duplicated(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch, "ai2.db")

    async def scenario():
        await db.init_db()
        from bot.database.repository import save_ai_thread, load_ai_thread
        async with db.SessionLocal() as s:
            await save_ai_thread(s, 1, 1, [{"role": "user", "content": "первый"}])
            await save_ai_thread(s, 1, 1, [{"role": "user", "content": "второй"}])
            return await load_ai_thread(s, 1)

    assert asyncio.run(scenario()) == [{"role": "user", "content": "второй"}]


def test_daily_limit_survives_restart(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch, "ai3.db")

    async def scenario():
        await db.init_db()
        from bot.database.repository import add_ai_usage, ai_usage_today
        async with db.SessionLocal() as s:
            for _ in range(3):
                await add_ai_usage(s, 42)
            await add_ai_usage(s, 7)
        async with db.SessionLocal() as s:          # «перезапуск»
            mine, total = await ai_usage_today(s, 42)
            other, _ = await ai_usage_today(s, 7)
            fresh, _ = await ai_usage_today(s, 999)
        await db.engine.dispose()
        return mine, total, other, fresh

    mine, total, other, fresh = asyncio.run(scenario())
    assert mine == 3            # раньше перезапуск обнулял счётчик
    assert total == 4           # общий лимит считает всех
    assert other == 1
    assert fresh == 0


def test_yesterdays_counters_are_dropped(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch, "ai4.db")

    async def scenario():
        await db.init_db()
        from bot.database.models import AiUsage
        from bot.database.repository import add_ai_usage, ai_usage_today
        from sqlalchemy import select, func
        async with db.SessionLocal() as s:
            s.add(AiUsage(day="2020-01-01", user_id=42, count=99))
            await s.commit()
            await add_ai_usage(s, 42)
            rows = (await s.execute(select(func.count(AiUsage.day)))).scalar()
            mine, _ = await ai_usage_today(s, 42)
        await db.engine.dispose()
        return rows, mine

    rows, mine = asyncio.run(scenario())
    assert rows == 1            # вчерашние записи не копятся
    assert mine == 1            # и не мешают сегодняшнему счёту
