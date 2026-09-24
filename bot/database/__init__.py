from sqlalchemy import Boolean, DateTime, Float, Integer, String, event, inspect
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


# Мягкие миграции: create_all не добавляет новые колонки в уже существующую таблицу,
# поэтому колонки, появившиеся позже, дописываем сами. (таблица, колонка, тип, значение
# по умолчанию как SQL-выражение или None).
#
# Раньше список колонок читался через «PRAGMA table_info» – это есть только у SQLite,
# и на любой другой базе бот падал бы на старте. Теперь спрашиваем SQLAlchemy
# (inspect), а тип колонки переводит в SQL сам диалект базы.
_ADDED_COLUMNS = [
    ("chat_settings", "slideshow_mode", String(), "'video'"),
    ("chat_settings", "currency_targets", String(), "'USD,EUR,UAH'"),
    ("chat_settings", "audio_track", Boolean(), "FALSE"),
    # NULL = не задано: дефолт решается по типу чата (группа/личка) в коде.
    ("chat_settings", "compress_shorts", Boolean(), None),
    # Отметка последней активности. У старых записей пустая – и это честно: мы правда
    # не знаем, когда эти люди последний раз пользовались ботом.
    ("users", "last_seen", DateTime(), None),
    # Кнопки, пережившие перезапуск, бывают трёх видов (качество, HDRezka, TikTok).
    ("stashed_links", "kind", String(), "'quality'"),
    ("stashed_links", "payload", String(), None),
    # Покупка бывает не только за звёзды: способ оплаты и сумма в долларах для крипты.
    ("payments", "method", String(), "'stars'"),
    ("payments", "usd", Float(), None),
    # Кэш file_id по каждому боту (id привязан к отправившему боту).
    ("cached_files", "bot_id", Integer(), None),
]


def _missing_columns(sync_conn) -> list[tuple[str, str, object, str | None]]:
    insp = inspect(sync_conn)
    tables = set(insp.get_table_names())
    have: dict[str, set[str]] = {}
    missing = []
    for table, column, col_type, default in _ADDED_COLUMNS:
        if table not in tables:
            continue
        if table not in have:
            have[table] = {c["name"] for c in insp.get_columns(table)}
        if column not in have[table]:
            missing.append((table, column, col_type, default))
    return missing


async def init_db():
    """Создаёт таблицы в БД, если их ещё нет, и дописывает новые колонки."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table, column, col_type, default in await conn.run_sync(_missing_columns):
            ddl = (f"ALTER TABLE {table} ADD COLUMN {column} "
                   f"{col_type.compile(dialect=conn.dialect)}")
            if default is not None:
                ddl += f" DEFAULT {default}"
            await conn.exec_driver_sql(ddl)
