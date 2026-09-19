from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from bot.config import DATABASE_URL
from bot.database.models import Base

engine = create_async_engine(DATABASE_URL)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    """Настройки SQLite, без которых база отказывает под нагрузкой.

    По умолчанию SQLite пускает к файлу либо одного пишущего, либо читающих — но не
    вместе. У нас в один момент пишут статистика, кэш file_id и счётчик трафика, а
    читают обработчики сообщений, и любой второй получал «database is locked».

      • journal_mode=WAL — пишущий больше не блокирует читающих (записи идут в
        отдельный журнал). Настройка ЗАПИСЫВАЕТСЯ В ФАЙЛ базы один раз и остаётся
        в нём навсегда, повторный вызов ничего не портит.
      • busy_timeout=5000 — если файл всё же занят, ждать до 5 секунд вместо
        мгновенного отказа. Дальше — ошибка, и это правильно: значит что-то зависло.
      • synchronous=NORMAL — обычный для WAL компромисс: при падении процесса данные
        целы, теряется максимум последняя транзакция при отключении питания.

    Вешаем на событие «подключились», а не выполняем один раз при старте: пул
    открывает соединения по мере надобности, и каждому новому нужны свои настройки
    (busy_timeout и synchronous живут в соединении, а не в файле).
    """
    if not DATABASE_URL.startswith("sqlite"):
        return
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


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
        if "compress_shorts" not in cols:
            # NULL = не задано: дефолт решается по типу чата (группа/личка) в коде.
            await conn.exec_driver_sql(
                "ALTER TABLE chat_settings ADD COLUMN compress_shorts BOOLEAN DEFAULT NULL"
            )

        # Кэш file_id теперь по каждому боту (id привязан к отправившему боту).
        res = await conn.exec_driver_sql("PRAGMA table_info(cached_files)")
        cf_cols = [r[1] for r in res.fetchall()]
        if "bot_id" not in cf_cols:
            await conn.exec_driver_sql(
                "ALTER TABLE cached_files ADD COLUMN bot_id INTEGER"
            )
