"""
Маршрутизация в общем моторе: несколько ботов крутятся на ОДНОМ диспетчере
(роутер нельзя привязать к нескольким диспетчерам). Этот фильтр пускает к хендлерам
только то, что умеет конкретный бот, и учитывает /setconfig для бота-комбайна.

  • viaSaver     — только скачивание;
  • viaVoice     — только транскрибация;
  • viaUnity     — всё + /setconfig; в личке работает как в группе (флаг «хаб»).
"""
from typing import Callable, Any
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, InlineQuery

from bot.database import SessionLocal
from bot.database.repository import get_disabled_features, current_bot_id
from bot.features.currency.parser import parse as parse_currency

GROUP_TYPES = ("group", "supergroup")


def _msg_feature(m: Message) -> str | None:
    """К какой функции относится сообщение (None = команда/прочее, не гейтим).
    Где именно разрешён конвертер (группа / личка хаба) — решает уже __call__."""
    if m.voice or m.video_note:
        return "transcribe"
    text = m.text or ""
    if text.startswith("http"):
        return "download"
    if text and parse_currency(text):
        return "currency"
    return None


class RoutingMiddleware(BaseMiddleware):
    def __init__(self, features_by_bot: dict[int, set[str]],
                 config_ids: set[int], hub_ids: set[int]):
        self.features_by_bot = features_by_bot
        self.config_ids = config_ids      # боты с /setconfig (в группах)
        self.hub_ids = hub_ids            # боты-хабы: в личке работают как в группе

    async def __call__(self, handler: Callable, event: Any, data: dict) -> Any:
        bot = getattr(event, "bot", None)
        bot_id = bot.id if bot else None
        # Кэш file_id ведётся по каждому боту — сообщаем репозиторию текущего бота.
        current_bot_id.set(bot_id)
        feats = self.features_by_bot.get(bot_id, set())
        is_config = bot_id in self.config_ids
        is_hub = bot_id in self.hub_ids
        # для /setconfig: набор функций бота и «хаб ли он» (чтобы работал в личке)
        data["bot_features"] = feats
        data["is_hub"] = is_hub

        feature = None
        chat = None

        if isinstance(event, Message):
            chat = event.chat
            if (event.text or "").startswith("/setconfig"):
                if not is_config:     # /setconfig только у ботов с конфигом
                    return
                return await handler(event, data)
            feature = _msg_feature(event)

        elif isinstance(event, CallbackQuery):
            if (event.data or "").startswith("cfg:"):
                if not is_config:     # переключатели конфига — только у ботов с конфигом
                    return
                return await handler(event, data)
            # прочие callback приходят с кнопок, которые появляются только у ботов
            # с нужной функцией — отдельно гейтить не нужно
            return await handler(event, data)

        elif isinstance(event, InlineQuery):
            if "download" not in feats:
                await event.answer([], cache_time=5, is_personal=True)
                return
            return await handler(event, data)

        if feature:
            if feature not in feats:          # бот не умеет эту функцию — молчим
                return
            in_group = chat and chat.type in GROUP_TYPES
            in_hub_dm = is_hub and chat and chat.type == "private"
            # Конвертер в личке — только у хаба
            if feature == "currency" and not in_group and not in_hub_dm:
                return
            # Выключенные функции: в группах у config-ботов и в личке у хаба
            if (is_config and in_group) or in_hub_dm:
                async with SessionLocal() as session:
                    disabled = await get_disabled_features(session, chat.id)
                if feature in disabled:       # выключено в этом чате через /setconfig
                    return
        return await handler(event, data)
