"""
«Отправляет видео…» в шапке чата, пока бот работает.

Зачем. Скачивание идёт секунды, а иногда и минуты. Всё это время человек видит только
своё сообщение и надпись-статус, и непонятно, живой бот или задумался. Telegram умеет
показывать в шапке чата «печатает…», «отправляет видео…» — ровно для таких случаев.
Надпись живёт пять секунд и продлевается сама, пока мы внутри блока.

Почему обёртка, а не прямой вызов ChatActionSender: действие в чате — приятная мелочь,
и она НЕ должна ломать работу. Права писать может не быть, чат мог быть удалён, сеть
могла мигнуть — во всех этих случаях мы просто продолжаем без надписи.
"""
import contextlib
import logging

logger = logging.getLogger(__name__)

# Что показывать. Названия — как у Telegram, чтобы не переизобретать словарь.
TYPING = "typing"
VIDEO = "upload_video"
PHOTO = "upload_photo"
DOCUMENT = "upload_document"
VOICE = "upload_voice"


@contextlib.asynccontextmanager
async def show(bot, chat_id: int, action: str = TYPING):
    """Показывает действие в шапке чата, пока выполняется блок.

    Пример:
        async with chat_action.show(bot, chat.id, chat_action.VIDEO):
            path = await download(...)
            await message.reply_video(...)
    """
    sender = None
    try:
        from aiogram.utils.chat_action import ChatActionSender
        sender = ChatActionSender(bot=bot, chat_id=chat_id, action=action)
        await sender.__aenter__()
    except Exception:
        sender = None                 # не вышло — работаем без надписи
    try:
        yield
    finally:
        if sender is not None:
            with contextlib.suppress(Exception):
                await sender.__aexit__(None, None, None)


async def once(bot, chat_id: int, action: str = TYPING) -> None:
    """Показывает действие ОДИН раз, без продления (Telegram держит его ~5 секунд).

    Нужно там, где обёртывать блок неудобно: например, скачивание трека — это длинная
    цепочка из поиска, загрузки и отправки тегов. Пять секунд всё равно лучше, чем
    полная тишина, а ошибку сюда пускать нельзя: действие в чате — мелочь, из-за
    которой не должна падать сама загрузка.
    """
    try:
        await bot.send_chat_action(chat_id=chat_id, action=action)
    except Exception:
        logger.debug("Не вышло показать действие в чате", exc_info=True)
