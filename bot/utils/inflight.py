"""
Координатор одновременных скачиваний одной и той же ссылки.

Задача: если одну ссылку быстро прислали в несколько чатов (например, в две
группы подряд), не качать один и тот же файл параллельно 10 раз. Первый запрос
становится «ведущим» и делает всю работу (скачивание, отправка, запись в кэш),
а остальные «ждут» его и, когда он закончит, просто берут готовый file_id из
кэша и переотправляют — без повторного скачивания.

Почему это ещё и чинит баг: раньше параллельные загрузки одного видео писали
файл под одним именем (по id видео). Первый успевал отправить и удалить файл, а
второй в этот момент пытался отправить уже удалённый файл → FileNotFoundError и
ложное «Не удалось скачать». Сериализация по ключу убирает эту гонку.

Ключ учитывает бота (current_bot_id): file_id в Telegram привязан к отправившему
боту, поэтому у разных ботов кэш свой — и ждать друг друга им незачем.
"""
import asyncio
import logging

from bot.database.repository import current_bot_id
from bot.utils.cache_guard import send_cached_or_drop

logger = logging.getLogger(__name__)

# Ключ (bot_id, url, cache_key) -> Event, который «ведущий» выставит по завершении.
_inflight: dict[tuple, asyncio.Event] = {}

# Сколько ждать ведущего, прежде чем сдаться и попробовать самому. Страховка на
# случай, если ведущий завис: скачивания и так ограничены таймаутами, но лучше
# не держать ожидающего вечно.
_WAIT_TIMEOUT = 300


def _key(url: str, cache_key: str) -> tuple:
    return (current_bot_id.get(), url, cache_key)


async def deduped(url, cache_key, get_cached, send_cached, produce):
    """
    Гарантирует, что одну и ту же работу (bot + url + cache_key) в один момент
    делает только один запрос; остальные ждут и переотправляют из кэша.

    Аргументы — короутины (async-функции без аргументов):
      get_cached()  -> значение из кэша или None;
      send_cached(v) -> переотправить готовое из кэша (то же, что при «кэш-хите»);
      produce()      -> скачать, отправить и сохранить в кэш (делает только ведущий).

    produce() сам обрабатывает свои ошибки (как и раньше в обработчиках): если
    скачать не вышло, он покажет пользователю сообщение об ошибке и НЕ сохранит
    кэш. Тогда ожидающие, не найдя кэша, попробуют по очереди сами.
    """
    cached = await get_cached()
    if cached is not None:
        # Если Telegram скажет, что такого файла у него нет, запись выбрасывается и
        # мы идём качать заново — как будто кэша и не было.
        if await send_cached_or_drop(lambda: send_cached(cached), url, cache_key):
            return

    key = _key(url, cache_key)
    while True:
        event = _inflight.get(key)
        if event is None:
            # Свободно — становимся ведущим и делаем работу.
            _inflight[key] = asyncio.Event()
            try:
                # Пока ждали своей очереди, кэш мог уже появиться — не дублируем.
                cached = await get_cached()
                if cached is not None and await send_cached_or_drop(
                        lambda: send_cached(cached), url, cache_key):
                    pass
                else:
                    await produce()
            finally:
                done = _inflight.pop(key, None)
                if done is not None:
                    done.set()
            return

        # Занято другим запросом — ждём и берём готовое из кэша.
        try:
            await asyncio.wait_for(event.wait(), _WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("Ожидание ведущей загрузки затянулось: %s", (url, cache_key))
        cached = await get_cached()
        if cached is not None and await send_cached_or_drop(
                lambda: send_cached(cached), url, cache_key):
            return
        # Ведущий не оставил кэша (ошибка/таймаут) — пробуем стать ведущим сами.
