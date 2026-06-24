import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from bot.config import BOT_TOKEN, TELEGRAM_LOCAL_API_URL, ADMIN_ID
from bot.database import init_db, SessionLocal
from bot.database.repository import get_stats
from bot.middlewares.register_user import RegisterUserMiddleware
from bot.handlers import start, link, admin
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


async def main():
    # Если указан локальный API сервер — используем его (для файлов >50MB)
    if TELEGRAM_LOCAL_API_URL:
        session = AiohttpSession(api=TelegramAPIServer.from_base(TELEGRAM_LOCAL_API_URL))
        bot = Bot(token=BOT_TOKEN, session=session)
    else:
        bot = Bot(token=BOT_TOKEN)

    dp = Dispatcher()

    # Подключаем middleware (авторегистрация пользователей)
    dp.message.middleware(RegisterUserMiddleware())

    # Подключаем хэндлеры
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(link.router)

    # Создаём таблицы в БД
    await init_db()

    # Чистим «хвосты» прошлых сессий (безопасно: ничего ещё не качается)
    clean_downloads()

    # Фоновая дневная задача (обновление yt-dlp + отчёт)
    asyncio.create_task(_daily_tasks(bot))

    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
