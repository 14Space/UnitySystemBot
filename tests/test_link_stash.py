"""Кнопки выбора качества должны переживать перезапуск бота."""
import asyncio
import importlib


def test_stash_survives_and_is_read_back(tmp_path, monkeypatch):
    import bot.config as config
    import bot.database as db

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    db = importlib.reload(db)

    async def scenario():
        await db.init_db()
        from bot.database.repository import save_link_stash, load_link_stash
        async with db.SessionLocal() as s:
            await save_link_stash(s, "abc123", "https://youtu.be/x", -100, 55, True)
        # «Перезапуск»: новая сессия, память пуста — данные должны найтись в базе.
        async with db.SessionLocal() as s:
            found = await load_link_stash(s, "abc123")
            missing = await load_link_stash(s, "нет-такого")
        await db.engine.dispose()
        return found, missing

    found, missing = asyncio.run(scenario())
    assert found == {"url": "https://youtu.be/x", "chat_id": -100,
                     "user_msg_id": 55, "premium": True, "kind": "quality"}
    assert missing is None
