"""
Скелет одной загрузки: кэш → слот → скачать → отправить → сохранить → убрать → освободить.

Зачем отдельным модулем. Этот порядок был переписан руками в каждом обработчике
link.py – восемь раз, и каждый раз чуть по-своему. Цена этого известна: 23.09.2026
бот замолчал насовсем, потому что в одной из копий отправка полоски стояла между
занятием слота и try, и слот не освободился. Уборка файла тоже жила то в finally, то
только на успешном пути – и фильм на гигабайты оставался на диске до перезапуска.

Теперь правильный порядок написан один раз:
  • slot()       – слот очереди, освобождается всегда;
  • exclusive()  – «у человека одна тяжёлая загрузка за раз», снимается всегда;
  • Cache        – где лежит готовая расписка Telegram (file_id) и как её сохранить;
  • produce()    – слот + работа + сохранение в кэш + уборка файлов;
  • run()        – то же, но сначала пробует кэш (и, по желанию, дедуп одной ссылки);
  • Progress     – полоска загрузки для тяжёлых видео.
Обработчику остаётся сказать, ЧТО качать и КАК отправлять.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from bot.database import SessionLocal
from bot.database.repository import (
    get_cached_file_id, save_cached_file_id, clear_cache_entry,
)
from bot.utils import files, inflight, limits
from bot.utils.cache_guard import send_cached_or_drop
from bot.utils.i18n import t
from bot.utils.progress_bar import make_progress_bar, ProgressThrottle
from bot.utils.tg_messages import safe_edit, safe_delete

logger = logging.getLogger(__name__)

# Пользователи с активной тяжёлой загрузкой – у каждого не больше одной. Читает и
# main.py: перед перезапуском ждёт, пока загрузки закончатся.
ACTIVE_DOWNLOADS: set[int] = set()


@asynccontextmanager
async def exclusive(user_id: int):
    """Отметка «этот человек уже качает» на время блока. Даёт True, если отметку
    поставили мы, и False, если загрузка у человека уже идёт (блок тогда должен
    просто сказать об этом и выйти).

    Проверка и отметка – без единого await между ними, то есть одним шагом для цикла
    событий. Раньше вызывающий спрашивал busy(), потом отвечал на нажатие и только
    потом ставил отметку – два быстрых нажатия проходили проверку оба.

    Снимается в любом случае: застрявшая отметка запирает человека до перезапуска
    («дождись текущей загрузки»). Чужую – ту, что поставил другой запрос, – не трогаем.
    """
    if user_id in ACTIVE_DOWNLOADS:
        yield False
        return
    ACTIVE_DOWNLOADS.add(user_id)
    try:
        yield True
    finally:
        ACTIVE_DOWNLOADS.discard(user_id)


@asynccontextmanager
async def slot(lane: str = limits.LIGHT, tell=None, lang: str = "ru"):
    """Слот очереди на время блока. tell – сообщение, на которое ответить «ты в
    очереди», если ждать придётся долго (None – ждать молча)."""
    token = await limits.acquire_or_tell(lane, tell, lang)
    try:
        yield
    finally:
        await limits.release(lane, token)


@dataclass(frozen=True)
class Cache:
    """Где лежит готовый результат: ссылка и вид (качество, «post», «audio»…).

    also – запасные ссылки-ключи. У TikTok одно видео приходит под разными короткими
    ссылками, поэтому храним и по самой ссылке (повтор без запроса к TikTok), и по
    номеру видео (разные ссылки на одно видео).
    """
    url: str
    key: str
    also: tuple[str, ...] = ()

    def _urls(self):
        return (self.url, *(u for u in self.also if u and u != self.url))

    async def get(self) -> str | None:
        async with SessionLocal() as session:
            for url in self._urls():
                value = await get_cached_file_id(session, url, self.key)
                if value:
                    return value
        return None

    async def save(self, value: str) -> None:
        async with SessionLocal() as session:
            for url in self._urls():
                await save_cached_file_id(session, url, value, self.key)

    async def drop(self) -> None:
        async with SessionLocal() as session:
            for url in self._urls():
                await clear_cache_entry(session, url, self.key)


async def try_cached(cache: Cache, send) -> bool:
    """Отправляет готовое из кэша. True – отправили; False – кэша нет или расписка
    оказалась мёртвой (тогда она удалена, и надо качать заново)."""
    value = await cache.get()
    if value is None:
        return False
    if await send_cached_or_drop(lambda: send(value), cache.url, cache.key):
        return True
    await cache.drop()              # запасные ключи хранят ту же мёртвую расписку
    return False


class Files(list):
    """Всё, что скачали за одну загрузку, – чтобы убрать это в finally разом."""

    def keep(self, item):
        """Запоминает путь (или список путей) и возвращает его же."""
        if item:
            self.append(item)
        return item


async def produce(*, cache: Cache | None, work, on_error, lane: str = limits.LIGHT,
                  tell=None, lang: str = "ru", label: str = "") -> None:
    """Слот → work → сохранить в кэш → убрать файлы → отпустить слот.

    work(paths: Files) – скачивает и отправляет, всё скачанное кладёт в paths.keep(...)
        и возвращает значение для кэша (file_id или набор) либо None.
    on_error(e) – говорит человеку о сбое; сам сбой уже записан в лог.
    """
    async with slot(lane, tell, lang):
        paths = Files()
        try:
            value = await work(paths)
            if value and cache is not None:
                await cache.save(value)
        except Exception as e:
            logger.exception("Загрузка не удалась: %s", label or (cache.url if cache else "?"))
            try:
                await on_error(e)
            except Exception:
                logger.warning("Не смог сообщить о сбое загрузки", exc_info=True)
        finally:
            # Уборка именно здесь: упала отправка (файл больше 2 ГБ, оборвалась связь,
            # у бота нет прав в чате) – а скачанное всё равно должно исчезнуть.
            files.remove(paths, record=True)


async def run(*, cache: Cache, send, work, on_error, lane: str = limits.LIGHT,
              tell=None, lang: str = "ru", label: str = "", dedupe: bool = False) -> None:
    """Кэш → иначе produce(). send(value) отправляет готовое из кэша.

    dedupe=True – одну и ту же ссылку в один момент качает только один запрос, а
    остальные ждут и берут его результат из кэша (см. bot.utils.inflight).
    """
    async def _produce():
        await produce(cache=cache, work=work, on_error=on_error, lane=lane,
                      tell=tell, lang=lang, label=label)

    if dedupe:
        await inflight.deduped(cache.url, cache.key, cache.get, send, _produce)
        return
    if await try_cached(cache, send):
        return
    await _produce()


class Progress:
    """Полоска загрузки тяжёлого видео в чате.

    Скачивание идёт в потоке, а править сообщение можно только из цикла событий –
    поэтому hook() и stage() перекидывают правку в цикл. Частоту ограничивает
    ProgressThrottle: правка на каждый процент – это до сотни правок, и Telegram
    отвечает «подожди».
    """

    def __init__(self, bot, chat_id: int, reply_to: int | None, lang: str):
        self.bot, self.chat_id, self.reply_to, self.lang = bot, chat_id, reply_to, lang
        self.msg = None
        self._loop = asyncio.get_running_loop()
        self._throttle = ProgressThrottle()

    async def start(self) -> None:
        self.msg = await self.bot.send_message(self.chat_id, make_progress_bar(0, self.lang),
                                               reply_to_message_id=self.reply_to)

    def _later(self, text: str) -> None:
        asyncio.run_coroutine_threadsafe(safe_edit(self.msg, text), self._loop)

    def hook(self, percent: int) -> None:
        """Для потока скачивания: новый процент."""
        if self._throttle.should_send(percent):
            self._later(make_progress_bar(percent, self.lang))

    def stage(self, key: str):
        """Для потока скачивания: функция, показывающая отдельный этап (склейку)."""
        return lambda: self._later(t(key, self.lang))

    async def say(self, key: str) -> None:
        await safe_edit(self.msg, t(key, self.lang))

    async def done(self) -> None:
        await safe_delete(self.msg)

    async def fail(self, text: str) -> None:
        """Показывает сбой в полоске, а если её нет (упало на её отправке) – отдельным
        сообщением."""
        if self.msg is not None and await safe_edit(self.msg, text):
            return
        await self.bot.send_message(self.chat_id, text)
