from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from bot.config import DATABASE_URL
from bot.database.models import Base

engine = create_async_engine(DATABASE_URL)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    """Создаёт таблицы в БД если их ещё нет"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
