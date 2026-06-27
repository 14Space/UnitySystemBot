import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from bot.config import BOT_TOKEN, TELEGRAM_LOCAL_API_URL, ADMIN_ID, WHISPER_PREWARM
from bot.database import init_db, SessionLocal
from bot.database.repository import get_stats
from bot.middlewares.register_user import RegisterUserMiddleware
from bot.middlewares.throttle import ThrottleMiddleware
from bot.handlers import start, link, admin, payment, inline, transcribe
from bot.handlers.admin import format_stats
from worker.maintenance import clean_downloads, update_ytdlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DAY_SECONDS = 24 * 60 * 60


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


async def _setup_commands(bot: Bot):
    """Меню-кнопки команд в Telegram (синяя кнопка «Меню» / список команд).
    Обычным пользователям — общие команды; админу в его чате — ещё и админские."""
    common = [
        # /start намеренно не в меню: он нужен один раз при первом запуске,
        # а сама команда продолжает работать (просто не висит в списке).
        BotCommand(command="help", description="Справка по командам"),
        BotCommand(command="premium", description="Купить Premium ✨"),
    ]
    await bot.set_my_commands(common, scope=BotCommandScopeDefault())
    if ADMIN_ID:
        admin_cmds = common + [
            BotCommand(command="statistics", description="Статистика"),
            BotCommand(command="cleancache", description="Очистить кэш"),
        ]
        await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))


async def main():
    # Если указан локальный API сервер — используем его (для файлов >50MB)
    if TELEGRAM_LOCAL_API_URL:
        session = AiohttpSession(api=TelegramAPIServer.from_base(TELEGRAM_LOCAL_API_URL))
        bot = Bot(token=BOT_TOKEN, session=session)
    else:
        bot = Bot(token=BOT_TOKEN)

    dp = Dispatcher()

    # Middleware: сначала анти-флуд, затем авторегистрация пользователей
    dp.message.middleware(ThrottleMiddleware())
    dp.message.middleware(RegisterUserMiddleware())

    # Подключаем хэндлеры
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(payment.router)
    dp.include_router(inline.router)
    dp.include_router(transcribe.router)
    dp.include_router(link.router)

    # Создаём таблицы в БД
    await init_db()

    # Меню-кнопки команд в интерфейсе Telegram
    await _setup_commands(bot)

    # Чистим «хвосты» прошлых сессий (безопасно: ничего ещё не качается)
    clean_downloads()

    # Фоновая дневная задача (обновление yt-dlp + отчёт)
    asyncio.create_task(_daily_tasks(bot))

    # Прогрев модели расшифровки: грузим в видеопамять заранее, в фоне, чтобы
    # не задерживать старт бота и чтобы первое голосовое не тормозило.
    if WHISPER_PREWARM:
        from worker.transcriber import warmup
        asyncio.create_task(asyncio.to_thread(warmup))

    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
