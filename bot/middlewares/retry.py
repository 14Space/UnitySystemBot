"""
Повтор запросов к Telegram, когда он просит подождать.

Telegram считает, сколько сообщений бот шлёт в единицу времени. При всплеске (альбом
из десяти фото, плейлист, слайдшоу) он отвечает «Too Many Requests: retry after 7» —
это не поломка, а вежливая просьба притормозить. Сам aiogram её не повторяет: запрос
просто падает с ошибкой, и человек недосчитывается части файлов.

Здесь мы ждём ровно столько, сколько попросили, и повторяем — до _MAX_TRIES раз.
Запросу, который ЗАЛИВАЕТ файл по сети, повторов достаётся меньше, а на обрыв связи
он не повторяется вовсе: полтора гигабайта заново — лекарство хуже болезни.
Заодно повторяем короткие сбои на стороне Telegram (500-е ответы и обрывы связи): они
почти всегда проходят со второй попытки, а раньше стоили пользователю целого файла.

Работает на уровне СЕССИИ, то есть охватывает любой вызов — отправку файла, правку
сообщения, ответ на кнопку, — а не только те места, где мы вспомнили про обработку.
"""
import asyncio
import logging

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramRetryAfter, TelegramServerError, TelegramNetworkError
from aiogram.types import InputFile

logger = logging.getLogger(__name__)

# Сколько раз пробуем всего (первая попытка + повторы).
_MAX_TRIES = 4
# Потолок ожидания: если Telegram просит ждать дольше, ждать смысла нет — человек
# уже решит, что бот сломался. Лучше честная ошибка.
_MAX_WAIT = 60
# Пауза перед повтором сетевого сбоя (у него, в отличие от лимита, срока нет).
_NETWORK_PAUSE = 2
# Сколько попыток даём запросу, который ЗАЛИВАЕТ файл по сети.
_MAX_TRIES_UPLOAD = 2


def _is_upload(method) -> bool:
    """Запрос тащит файл по сети (а не ссылку file:// и не готовый file_id)?

    Разница принципиальная. Обычный вызов — это несколько сотен байт, повторить его
    ничего не стоит. А отправка видео на полтора гигабайта — это полтора гигабайта
    ПО СЕТИ заново, и слепой повтор такого запроса превращает одну неудачу в четыре
    заливки подряд: канал забит, человек ждёт втрое дольше, а причина сбоя (обрыв на
    середине) от повторов не исчезает.

    В локальном режиме файл отдаётся строкой «file://путь», сервер Bot API читает его
    с диска — там заливки нет, и такой запрос повторять не жалко.
    """
    for name in getattr(type(method), "model_fields", {}):
        value = getattr(method, name, None)
        if isinstance(value, InputFile):
            return True
        if isinstance(value, (list, tuple)):
            if any(isinstance(v, InputFile) or isinstance(getattr(v, "media", None), InputFile)
                   for v in value):
                return True
        if isinstance(getattr(value, "media", None), InputFile):
            return True
    return False


class RetryAfterMiddleware(BaseMiddleware):
    """Middleware сессии: повторяет запрос, если Telegram попросил подождать."""

    async def __call__(self, make_request, bot, method):
        upload = _is_upload(method)
        tries = _MAX_TRIES_UPLOAD if upload else _MAX_TRIES
        for attempt in range(1, tries + 1):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as e:
                if attempt == tries or e.retry_after > _MAX_WAIT:
                    raise
                # Полсекунды сверху: если проснуться ровно в срок, лимит иногда ещё
                # не успевает отпустить, и повтор ловит ту же ошибку.
                wait = e.retry_after + 0.5
                logger.info("Telegram просит подождать %sс (%s) — попытка %d/%d",
                            e.retry_after, type(method).__name__, attempt, tries)
                await asyncio.sleep(wait)
            except (TelegramServerError, TelegramNetworkError) as e:
                # Сбой связи посреди заливки файла НЕ повторяем: файл почти наверняка
                # уехал наполовину, и повтор — это ещё одна полная заливка по тому же
                # каналу, который только что оборвался.
                if attempt == tries or upload:
                    raise
                logger.info("Сбой связи с Telegram (%s: %s) — повтор через %dс",
                            type(e).__name__, e, _NETWORK_PAUSE)
                await asyncio.sleep(_NETWORK_PAUSE)
