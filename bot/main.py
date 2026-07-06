import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from bot.config import (
    BOT_TOKEN, TRANSCRIBE_BOT_TOKEN, UNITY_BOT_TOKEN,
    TELEGRAM_LOCAL_API_URL, ADMIN_ID, WHISPER_PREWARM,
)
from bot.database import init_db, SessionLocal
from bot.database.repository import get_stats, add_traffic
from bot.utils import traffic
from bot.middlewares.register_user import RegisterUserMiddleware
from bot.middlewares.throttle import ThrottleMiddleware
from bot.middlewares.routing import RoutingMiddleware
from bot.features.common import start, admin, payment, inline
from bot.features.download import link
from bot.features.transcribe import transcribe
from bot.features.config import setconfig
from bot.features.common.admin import format_stats
from bot.features.download.maintenance import clean_downloads, update_ytdlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DAY_SECONDS = 24 * 60 * 60

# Что умеет бот (набор функций). По ним маршрутизатор пускает нужные апдейты.
DOWNLOAD = "download"
TRANSCRIBE = "transcribe"
# CURRENCY = "currency"  # добавим позже


def _bot_configs() -> list[dict]:
    """Список ботов к запуску. Запускаем только тех, у кого задан токен.
    Пока нет доп. токенов — @viaSaver умеет всё (чтобы не потерять расшифровку).
    Как только появятся остальные — @viaSaver становится «только скачивание»."""
    others = bool(TRANSCRIBE_BOT_TOKEN) or bool(UNITY_BOT_TOKEN)
    saver_features = {DOWNLOAD} if others else {DOWNLOAD, TRANSCRIBE}

    bots = [{"name": "viaSaver", "token": BOT_TOKEN, "features": saver_features, "config": False}]
    if TRANSCRIBE_BOT_TOKEN:
        bots.append({"name": "viaVoice", "token": TRANSCRIBE_BOT_TOKEN,
                     "features": {TRANSCRIBE}, "config": False})
    if UNITY_BOT_TOKEN:
        bots.append({"name": "viaUnity", "token": UNITY_BOT_TOKEN,
                     "features": {DOWNLOAD, TRANSCRIBE}, "config": True})
    return bots


def _make_bot(token: str) -> Bot:
    """Создаёт Bot; если задан локальный API-сервер — с ним (файлы до 2 ГБ)."""
    if TELEGRAM_LOCAL_API_URL:
        session = AiohttpSession(api=TelegramAPIServer.from_base(TELEGRAM_LOCAL_API_URL))
        return Bot(token=token, session=session)
    return Bot(token=token)


async def _setup_commands(bot: Bot, features: set[str], with_config: bool):
    """Меню команд под конкретного бота (у скачивателя — Premium и т.д.)."""
    common = [BotCommand(command="help", description="Справка по командам")]
    if DOWNLOAD in features:
        common.append(BotCommand(command="premium", description="Купить Premium ✨"))
    if with_config:
        common.append(BotCommand(command="setconfig", description="Настроить функции (в группе)"))
    await bot.set_my_commands(common, scope=BotCommandScopeDefault())
    if ADMIN_ID:
        admin_cmds = common + [
            BotCommand(command="statistics", description="Статистика"),
            BotCommand(command="cleancache", description="Очистить кэш"),
        ]
        # Личный чат админа с ботом может ещё не существовать (админ не писал этому
        # боту) — тогда Telegram вернёт «chat not found». Не роняем из-за этого запуск.
        try:
            await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
        except Exception:
            logger.info("Не задал админ-команды для %s (админ ещё не писал боту)", bot.id)


async def _daily_tasks(bot: Bot):
    """Раз в сутки: обновляем yt-dlp и шлём админу отчёт по статистике."""
    while True:
        await asyncio.sleep(DAY_SECONDS)
        await asyncio.to_thread(update_ytdlp)
        if ADMIN_ID:
            try:
                async with SessionLocal() as session:
                    stats = await get_stats(session)
                await bot.send_message(ADMIN_ID, format_stats(stats), parse_mode="HTML")
            except Exception:
                logger.exception("Не удалось отправить дневной отчёт")


async def _flush_traffic():
    """Раз в минуту сбрасываем накопленный трафик (скачанные байты) в БД."""
    while True:
        await asyncio.sleep(60)
        nbytes, nfiles = traffic.take()
        if nbytes:
            try:
                async with SessionLocal() as session:
                    await add_traffic(session, nbytes, nfiles)
            except Exception:
                logger.exception("Не удалось сохранить статистику трафика")


async def main():
    await init_db()
    clean_downloads()  # чистим «хвосты» прошлых сессий

    # Поднимаем всех ботов, у кого есть токен
    bots: list[Bot] = []
    features_by_bot: dict[int, set[str]] = {}
    unity_ids: set[int] = set()
    any_transcribe = False
    for cfg in _bot_configs():
        bot = _make_bot(cfg["token"])
        bots.append(bot)
        features_by_bot[bot.id] = cfg["features"]
        if cfg["config"]:
            unity_ids.add(bot.id)
        any_transcribe = any_transcribe or (TRANSCRIBE in cfg["features"])
        await _setup_commands(bot, cfg["features"], cfg["config"])
        logger.info("Бот %s (id=%s): функции=%s, config=%s",
                    cfg["name"], bot.id, cfg["features"], cfg["config"])

    # Один диспетчер на всех ботов; маршрутизатор раздаёт функции по ботам/группам
    dp = Dispatcher()
    dp.message.middleware(ThrottleMiddleware())
    dp.message.middleware(RegisterUserMiddleware())
    routing = RoutingMiddleware(features_by_bot, unity_ids)
    dp.message.middleware(routing)
    dp.callback_query.middleware(routing)
    dp.inline_query.middleware(routing)

    # Все роутеры подключаем один раз (маршрутизатор отфильтрует лишнее по каждому боту)
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(setconfig.router)
    dp.include_router(transcribe.router)
    dp.include_router(payment.router)
    dp.include_router(inline.router)
    dp.include_router(link.router)

    asyncio.create_task(_daily_tasks(bots[0]))
    asyncio.create_task(_flush_traffic())
    if WHISPER_PREWARM and any_transcribe:
        from bot.features.transcribe.transcriber import warmup
        asyncio.create_task(asyncio.to_thread(warmup))

    print(f"Запущено ботов: {len(bots)}")
    await dp.start_polling(*bots)


if __name__ == "__main__":
    asyncio.run(main())
