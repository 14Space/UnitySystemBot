"""
Что делать, когда file_id из кэша больше не работает.

Кэш — это не файл, а РАСПИСКА: «такой-то файл лежит у Telegram под таким номером».
Обычно номер живёт вечно, но не всегда: файл могли удалить на стороне Telegram,
расписка могла остаться от другого бота или от сервера Bot API, который переставили
заново. Тогда на отправку прилетает «wrong file identifier».

Раньше это выглядело как полное молчание: отправка падала, пользователь не получал
ни файла, ни ошибки. Теперь такая расписка выбрасывается из кэша, и вызывающий код
качает заново — то есть сам себя чинит с первого же повтора.

Чужие ошибки (нет прав, чат не найден, слишком длинная подпись) сюда не попадают:
их пробрасываем как есть, потому что перекачиванием они не лечатся.
"""
import logging

from aiogram.exceptions import TelegramBadRequest

from bot.database import SessionLocal
from bot.database.repository import clear_cache_entry

logger = logging.getLogger(__name__)

# Как Telegram сообщает, что этот номер файла ему ни о чём не говорит.
_DEAD_FILE_ID = (
    "wrong file identifier",
    "wrong remote file identifier",
    "wrong file_id",
    "file_id invalid",
    "file reference expired",
    "file is temporarily unavailable",
)


def is_dead_file_id(error: Exception) -> bool:
    """Это именно «номер файла не годится», а не какая-то другая жалоба Telegram?"""
    if not isinstance(error, TelegramBadRequest):
        return False
    text = str(error).lower()
    return any(sign in text for sign in _DEAD_FILE_ID)


async def send_cached_or_drop(send, url: str, cache_key: str | None) -> bool:
    """Отправляет готовое из кэша. True — получилось; False — расписка оказалась
    мёртвой, запись удалена, и вызывающему следует скачать заново.

    send — корутина без аргументов (сама знает, что и куда слать)."""
    try:
        await send()
        return True
    except Exception as e:
        if not is_dead_file_id(e):
            raise
        logger.info("Кэш протух (%s | %s): %s — чищу и качаю заново", url, cache_key, e)
        async with SessionLocal() as session:
            await clear_cache_entry(session, url, cache_key)
        return False
