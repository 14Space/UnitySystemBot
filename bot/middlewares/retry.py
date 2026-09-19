"""
Повтор запросов к Telegram, когда он просит подождать.

Telegram считает, сколько сообщений бот шлёт в единицу времени. При всплеске (альбом
из десяти фото, плейлист, слайдшоу) он отвечает «Too Many Requests: retry after 7» —
это не поломка, а вежливая просьба притормозить. Сам aiogram её не повторяет: запрос
просто падает с ошибкой, и человек недосчитывается части файлов.

Здесь мы ждём ровно столько, сколько попросили, и повторяем — до _MAX_TRIES раз.
Заодно повторяем короткие сбои на стороне Telegram (500-е ответы и обрывы связи): они
почти всегда проходят со второй попытки, а раньше стоили пользователю целого файла.

Работает на уровне СЕССИИ, то есть охватывает любой вызов — отправку файла, правку
сообщения, ответ на кнопку, — а не только те места, где мы вспомнили про обработку.
"""
import asyncio
import logging

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramRetryAfter, TelegramServerError, TelegramNetworkError

logger = logging.getLogger(__name__)

# Сколько раз пробуем всего (первая попытка + повторы).
_MAX_TRIES = 4
# Потолок ожидания: если Telegram просит ждать дольше, ждать смысла нет — человек
# уже решит, что бот сломался. Лучше честная ошибка.
_MAX_WAIT = 60
# Пауза перед повтором сетевого сбоя (у него, в отличие от лимита, срока нет).
_NETWORK_PAUSE = 2


class RetryAfterMiddleware(BaseMiddleware):
    """Middleware сессии: повторяет запрос, если Telegram попросил подождать."""

    async def __call__(self, make_request, bot, method):
        for attempt in range(1, _MAX_TRIES + 1):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as e:
                if attempt == _MAX_TRIES or e.retry_after > _MAX_WAIT:
                    raise
                # Полсекунды сверху: если проснуться ровно в срок, лимит иногда ещё
                # не успевает отпустить, и повтор ловит ту же ошибку.
                wait = e.retry_after + 0.5
                logger.info("Telegram просит подождать %sс (%s) — попытка %d/%d",
                            e.retry_after, type(method).__name__, attempt, _MAX_TRIES)
                await asyncio.sleep(wait)
            except (TelegramServerError, TelegramNetworkError) as e:
                if attempt == _MAX_TRIES:
                    raise
                logger.info("Сбой связи с Telegram (%s: %s) — повтор через %dс",
                            type(e).__name__, e, _NETWORK_PAUSE)
                await asyncio.sleep(_NETWORK_PAUSE)
