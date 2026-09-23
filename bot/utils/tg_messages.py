"""
Правка и удаление своих сообщений, которые не роняют обработчик.

Сообщение «Скачиваю…» или «Расшифровываю…» к моменту правки могли удалить, у бота
могли отобрать право писать, а Telegram отвечает «message is not modified», если текст
не изменился. Ничто из этого не повод падать посреди загрузки. Раньше такая обёртка
была написана трижды (ссылки, ИИ, расшифровка) – с разными ответами на выходе.
"""
import logging

logger = logging.getLogger(__name__)


async def safe_edit(msg, text: str, parse_mode: str | None = None):
    """Правит текст сообщения. Возвращает то, что вернул Telegram, или None, если не
    вышло – тогда вызывающий сам решает, доставить ли текст другим путём."""
    if msg is None:
        return None
    try:
        return await msg.edit_text(text, parse_mode=parse_mode)
    except Exception as e:
        logger.info("Не смог отредактировать сообщение: %s", str(e)[:120])
        return None


async def safe_delete(msg) -> None:
    """Удаляет сообщение, если это ещё возможно."""
    if msg is None:
        return
    try:
        await msg.delete()
    except Exception:
        pass
