from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from bot.config import DATABASE_URL
from bot.database.models import Base

engine = create_async_engine(DATABASE_URL)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    """Создаёт таблицы в БД если их ещё нет"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Мягкая миграция: create_all не добавляет новые колонки в уже существующую
        # таблицу. Дописываем slideshow_mode вручную, если её ещё нет.
        res = await conn.exec_driver_sql("PRAGMA table_info(chat_settings)")
        cols = [r[1] for r in res.fetchall()]
        if "slideshow_mode" not in cols:
            await conn.exec_driver_sql(
                "ALTER TABLE chat_settings ADD COLUMN slideshow_mode VARCHAR DEFAULT 'video'"
            )
        if "currency_targets" not in cols:
            await conn.exec_driver_sql(
                "ALTER TABLE chat_settings ADD COLUMN currency_targets VARCHAR DEFAULT 'USD,EUR,UAH'"
            )
        if "audio_track" not in cols:
            await conn.exec_driver_sql(
                "ALTER TABLE chat_settings ADD COLUMN audio_track BOOLEAN DEFAULT 0"
            )
