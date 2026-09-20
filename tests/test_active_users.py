"""«Активных за месяц» — те, кто реально обращался к боту.

Зачем отдельно от «всего»: цифра «всего» историческая. В ней и те, кто когда-то просто
оказался в группе с ботом, — раньше в базу писался каждый, кто написал там хоть что-то.
"""
import asyncio
import importlib
from datetime import datetime, timedelta, timezone


def _fresh_db(tmp_path, monkeypatch, name):
    import bot.config as config
    import bot.database as db

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / name}")
    return importlib.reload(db)


def test_active_counts_only_recent(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch, "act.db")

    async def scenario():
        await db.init_db()
        from bot.database.models import User
        from bot.database.repository import get_stats
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with db.SessionLocal() as s:
            s.add(User(user_id=1, username="свежий", last_seen=now))
            s.add(User(user_id=2, username="вчерашний", last_seen=now - timedelta(days=1)))
            s.add(User(user_id=3, username="давний", last_seen=now - timedelta(days=40)))
            s.add(User(user_id=4, username="из старых записей"))      # отметки нет вовсе
            await s.commit()
            stats = await get_stats(s)
        await db.engine.dispose()
        return stats

    stats = asyncio.run(scenario())
    assert stats["users"] == 4          # всего — как было
    assert stats["active"] == 2         # только те, кто заходил за месяц


def test_activity_is_marked_and_not_rewritten_every_message(tmp_path, monkeypatch):
    """Отметку обновляем не чаще раза в полчаса: десять ссылок подряд — это один
    активный человек, а запись в базу на каждое сообщение ничего не уточняет."""
    db = _fresh_db(tmp_path, monkeypatch, "seen.db")

    async def scenario():
        await db.init_db()
        from bot.database.repository import get_or_create_user
        async with db.SessionLocal() as s:
            user = await get_or_create_user(s, 77, "новичок")
            first = user.last_seen
            assert first is not None                    # новый сразу активен

            user.last_seen = first - timedelta(minutes=5)   # как будто писал 5 минут назад
            await s.commit()
            again = await get_or_create_user(s, 77, "новичок")
            fresh_enough = again.last_seen

            user.last_seen = first - timedelta(hours=2)      # а теперь два часа назад
            await s.commit()
            updated = await get_or_create_user(s, 77, "новичок")
            after_long_pause = updated.last_seen
        await db.engine.dispose()
        return first, fresh_enough, after_long_pause

    first, fresh_enough, after_long_pause = asyncio.run(scenario())
    assert fresh_enough < first                 # недавнюю отметку не переписали
    assert after_long_pause > fresh_enough      # а спустя паузу — обновили


def test_migration_adds_the_column(tmp_path, monkeypatch):
    """База, созданная до появления отметки, должна получить колонку при старте."""
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE, "
                "username VARCHAR, is_premium BOOLEAN, language VARCHAR, created_at DATETIME)")
    con.execute("INSERT INTO users (user_id, username) VALUES (5, 'старожил')")
    con.commit()
    con.close()

    db = _fresh_db(tmp_path, monkeypatch, "old.db")
    asyncio.run(db.init_db())
    asyncio.run(db.engine.dispose())

    con = sqlite3.connect(path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(users)")]
    rows = con.execute("SELECT username, last_seen FROM users").fetchall()
    con.close()
    assert "last_seen" in cols
    assert rows == [("старожил", None)]    # старым записям отметку не выдумываем
