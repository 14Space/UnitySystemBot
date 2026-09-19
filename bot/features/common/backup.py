"""
Резервная копия базы — раз в сутки, вместе с отчётом.

В базе лежит всё, что бот накопил сам: кэш file_id, статистика, настройки чатов,
кто купил премиум и номера платежей. Восстановить это неоткуда — площадки такого
не хранят. Поэтому копия каждый день уезжает админу в Telegram: сервер может
умереть целиком, а переписка с ботом останется.

Как снимаем копию. Не «скопировать файл»: база живая, в этот момент в неё могут
писать, и обычная копия получилась бы наполовину устаревшей (а с журналом WAL — ещё
и неполной). Команда SQLite «VACUUM INTO» делает согласованный снимок: она читает
базу целиком в одном состоянии и попутно ужимает — файл получается меньше исходного
и открывается как обычная база.
"""
import logging
import os
import tempfile
from datetime import datetime

from bot.database import engine

logger = logging.getLogger(__name__)


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d")


async def dump_database() -> str | None:
    """Делает снимок базы во временный файл и возвращает путь (или None при сбое).
    Удалять файл — задача вызывающего, он же его и отправляет."""
    folder = tempfile.gettempdir()
    path = os.path.join(folder, f"unitysystem-{_stamp()}.db")
    # Старый снимок за то же число мешает: VACUUM INTO отказывается писать в
    # существующий файл. Он уже отправлен, так что просто убираем.
    try:
        os.remove(path)
    except OSError:
        pass
    try:
        async with engine.connect() as conn:
            # Путь подставляем в текст команды: VACUUM INTO не принимает параметры.
            # Кавычки внутри пути удваиваем — это экранирование в SQL-строке.
            safe = path.replace("'", "''")
            await conn.exec_driver_sql(f"VACUUM INTO '{safe}'")
        return path
    except Exception:
        logger.exception("Не удалось снять резервную копию базы")
        return None
