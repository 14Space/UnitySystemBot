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
import zipfile
from datetime import datetime

from bot import config
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


def _secret_files() -> list[tuple[str, str]]:
    """(путь на диске, имя в архиве) – всё, без чего бота не поднять заново.

    База восстанавливает то, что бот накопил, а это – то, с чем он вообще запускается:
    токены и ключи (.env), живые входы на площадки (куки), сессия аккаунта-посредника
    и ключи туннеля до дома. Одноразовые копии кук (cookie-copies) не берём: это те же
    куки, только устаревшие.
    """
    out = [(config.SECRETS_ENV_FILE, ".env")]
    for path in (config.INSTAGRAM_COOKIES, config.X_COOKIES, config.YOUTUBE_COOKIES,
                 config.YOUTUBE_COOKIES + ".work", config.RELAY_STT_SESSION):
        out.append((path, os.path.basename(path)))
    try:
        for name in sorted(os.listdir(config.WIREGUARD_DIR)):
            out.append((os.path.join(config.WIREGUARD_DIR, name), f"wireguard/{name}"))
    except OSError:
        pass                                   # туннель не настроен или не примонтирован
    return [(p, a) for p, a in out if p and os.path.isfile(p)]


def dump_secrets() -> tuple[str | None, list[str]]:
    """Архив секретов во временный файл: (путь или None, что в него попало).
    Удалять файл – задача вызывающего, он же его и отправляет."""
    found = _secret_files()
    if not found:
        return None, []
    path = os.path.join(tempfile.gettempdir(), f"unitysystem-secrets-{_stamp()}.zip")
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for src, arcname in found:
                zf.write(src, arcname=arcname)
        return path, [a for _, a in found]
    except Exception:
        logger.exception("Не удалось собрать архив секретов")
        try:
            os.remove(path)
        except OSError:
            pass
        return None, []

