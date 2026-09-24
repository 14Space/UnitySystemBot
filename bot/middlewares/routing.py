"""
Применяет настройки /setconfig: бот умеет всё, но в конкретном чате часть функций
может быть выключена (админом группы или самим пользователем в личке). Если функция
выключена — молча пропускаем сообщение мимо хендлеров. Здесь же сообщаем репозиторию
id текущего бота: кэш file_id ведётся по боту (id чужого бота невалиден).
"""
from typing import Callable, Any
from aiogram import BaseMiddleware
from aiogram.types import Message

from bot.database import SessionLocal
from bot.database.repository import get_disabled_features, current_bot_id
from bot.features.currency.parser import parse as parse_currency
from bot.utils.platform_detector import detect_platform, Platform, extract_url


def _msg_feature(m: Message) -> str | None:
    """К какой функции относится сообщение (None = команда/прочее, не гейтим)."""
    if m.voice or m.video_note:
        return "transcribe"
    # Текст И ПОДПИСЬ: ссылку часто присылают подписью к пересланному видео или
    # картинке. Раньше такое сообщение для нас было пустым — бот молчал, и человек
    # не понимал, почему та же ссылка текстом работает, а подписью нет.
    text = m.text or m.caption or ""
    # Именно НАША площадка, а не любая ссылка: чужие бот всё равно не качает, и
    # считать их обращением к нему неправильно — по этому признаку человек попадал
    # в статистику и занимал место в очереди, ничего у бота не попросив.
    link = extract_url(text)
    if link and detect_platform(link) != Platform.UNKNOWN:
        return "download"
    if text and parse_currency(text):
        return "currency"
    # Ответ реплаем на сообщение бота — продолжение разговора с ИИ (см. features/ai).
    # Проверяем последним: реплай со ссылкой или валютой — это всё-таки скачивание и
    # конвертер, они разобрались выше. Здесь нам важно, что такой реплай ВООБЩЕ
    # считается обращением к боту: иначе он не попадёт ни в очередь, ни под тумблер
    # «ИИ-ассистент», ни в статистику — для них он выглядел бы чужой болтовнёй.
    replied = getattr(m, "reply_to_message", None)
    # from_user может не быть вовсе: анонимный админ группы, пост из канала.
    author = getattr(replied, "from_user", None)
    if text and not text.startswith("/") and getattr(author, "is_bot", False):
        return "ai"
    return None


# Ключ, под которым ответ «к какой функции относится сообщение» лежит в общих данных
# апдейта. Считать его несколько раз нельзя: разбор валюты — самая дорогая часть, а
# зовут её три посредника подряд (очередь, учёт пользователя, маршрутизация). Общий
# словарь data живёт ровно один апдейт, поэтому лучшего места для ответа нет.
_FEATURE_KEY = "_msg_feature"


def feature_of(m: Message, data: dict | None = None) -> str | None:
    """К какой функции относится сообщение. Считается ОДИН раз на апдейт."""
    if data is None:
        return _msg_feature(m)
    if _FEATURE_KEY not in data:
        data[_FEATURE_KEY] = _msg_feature(m)
    return data[_FEATURE_KEY]


def is_request(m: Message, data: dict | None = None) -> bool:
    """Это обращение К БОТУ, а не обычная переписка в чате?

    Считаем обращением команду, ссылку, голосовое/кружок и валютный запрос. Всё
    остальное — чужой разговор, который бот просто видит, потому что состоит в группе.
    """
    if (m.text or "").startswith("/"):
        return True
    return feature_of(m, data) is not None


class RoutingMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable, event: Any, data: dict) -> Any:
        bot = getattr(event, "bot", None)
        current_bot_id.set(bot.id if bot else None)

        if isinstance(event, Message):
            feature = feature_of(event, data)
            if feature:
                async with SessionLocal() as session:
                    disabled = await get_disabled_features(session, event.chat.id)
                if feature in disabled:      # выключено в этом чате через /setconfig
                    return
        return await handler(event, data)
