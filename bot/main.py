import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import (
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
    BotCommandScopeAllChatAdministrators, BotCommandScopeAllPrivateChats,
)
from bot.config import (
    BOT_TOKEN, TRANSCRIBE_BOT_TOKEN, UNITY_BOT_TOKEN, CURRENCY_BOT_TOKEN,
    TELEGRAM_LOCAL_API_URL, ADMIN_ID, WHISPER_PREWARM,
)
from bot.database import init_db, SessionLocal
from bot.database.repository import get_stats, add_traffic
from bot.utils import traffic
from bot.utils.i18n import t
from bot.middlewares.register_user import RegisterUserMiddleware
from bot.middlewares.throttle import ThrottleMiddleware
from bot.middlewares.routing import RoutingMiddleware
from bot.features.common import start, admin, payment, inline
from bot.features.download import link
from bot.features.transcribe import transcribe
from bot.features.currency import convert as currency
from bot.features.ai import chat as ai_chat
from bot.features.config import setconfig
from bot.features.common.admin import format_stats
from bot.features.download.maintenance import clean_downloads, update_ytdlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DAY_SECONDS = 24 * 60 * 60

# Что умеет бот (набор функций). По ним маршрутизатор пускает нужные апдейты.
DOWNLOAD = "download"
TRANSCRIBE = "transcribe"
CURRENCY = "currency"
AI = "ai"


def _bot_configs() -> list[dict]:
    """Список ботов к запуску. Запускаем только тех, у кого задан токен.
    Пока нет доп. токенов — @viaSaver умеет всё (чтобы не потерять расшифровку).
    Как только появятся остальные — @viaSaver становится «только скачивание»."""
    others = bool(TRANSCRIBE_BOT_TOKEN) or bool(UNITY_BOT_TOKEN) or bool(CURRENCY_BOT_TOKEN)
    saver_features = {DOWNLOAD} if others else {DOWNLOAD, TRANSCRIBE, CURRENCY, AI}

    # «hub» — бот-комбайн: в личке работает как в группе (конвертер + личный /setconfig).
    # В одиночном режиме хабом становится сам viaSaver (он умеет всё).
    bots = [{"name": "viaSaver", "token": BOT_TOKEN, "features": saver_features,
             "config": True, "hub": not others}]
    if TRANSCRIBE_BOT_TOKEN:
        bots.append({"name": "viaVoice", "token": TRANSCRIBE_BOT_TOKEN,
                     "features": {TRANSCRIBE}, "config": False, "hub": False})
    if CURRENCY_BOT_TOKEN:
        bots.append({"name": "viaCurrency", "token": CURRENCY_BOT_TOKEN,
                     "features": {CURRENCY}, "config": False, "hub": False})
    if UNITY_BOT_TOKEN:
        bots.append({"name": "viaUnity", "token": UNITY_BOT_TOKEN,
                     "features": {DOWNLOAD, TRANSCRIBE, CURRENCY, AI}, "config": True, "hub": True})
    return bots


def _make_bot(token: str) -> Bot:
    """Создаёт Bot; если задан локальный API-сервер — с ним (файлы до 2 ГБ)."""
    if TELEGRAM_LOCAL_API_URL:
        session = AiohttpSession(api=TelegramAPIServer.from_base(TELEGRAM_LOCAL_API_URL))
        return Bot(token=token, session=session)
    return Bot(token=token)


async def _setup_commands(bot: Bot, features: set[str], with_config: bool, is_hub: bool):
    """Меню команд под конкретного бота (у скачивателя — Premium и т.д.)."""
    common = [BotCommand(command="help", description="Справка по командам")]
    if AI in features:
        common.append(BotCommand(command="ai", description="Спросить ИИ"))
    if DOWNLOAD in features:
        common.append(BotCommand(command="premium", description="Купить Premium ✨"))
    await bot.set_my_commands(common, scope=BotCommandScopeDefault())
    # /setconfig показываем ТОЛЬКО админам групп: этот scope действует лишь в группах
    # и только для их администраторов. У обычных участников команда в меню не появляется.
    if with_config:
        setconfig_cmd = BotCommand(command="setconfig", description="Настроить функции")
        await bot.set_my_commands(common + [setconfig_cmd],
                                  scope=BotCommandScopeAllChatAdministrators())
        # У хаба /setconfig есть и в личке (личные настройки) — показываем в меню лички.
        if is_hub:
            await bot.set_my_commands(common + [setconfig_cmd],
                                      scope=BotCommandScopeAllPrivateChats())
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


def _profile_role(features: set[str]) -> str | None:
    """Какой профиль (About/Description) ставить боту. viaSaver настроен вручную —
    его не трогаем (None)."""
    if features == {TRANSCRIBE}:
        return "voice"
    if features == {DOWNLOAD}:
        return None
    return "unity"


async def _setup_profile(bot: Bot, features: set[str]):
    """Ставит короткое описание (About) и описание (экран до Start) по языкам ru/uk/en,
    плюс английский по умолчанию. Для viaSaver пропускаем."""
    role = _profile_role(features)
    if not role:
        return
    for lang in ("ru", "uk", "en"):
        try:
            await bot.set_my_short_description(short_description=t(f"about_{role}", lang), language_code=lang)
            await bot.set_my_description(description=t(f"desc_{role}", lang), language_code=lang)
        except Exception:
            logger.exception("Не задал профиль (%s) для %s", lang, bot.id)
    try:  # дефолт для остальных языков — английский
        await bot.set_my_short_description(short_description=t(f"about_{role}", "en"))
        await bot.set_my_description(description=t(f"desc_{role}", "en"))
    except Exception:
        logger.exception("Не задал дефолтный профиль для %s", bot.id)


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
    config_ids: set[int] = set()          # боты с доступом к /setconfig
    hub_ids: set[int] = set()             # боты-хабы: в личке работают как в группе
    any_transcribe = False
    for cfg in _bot_configs():
        bot = _make_bot(cfg["token"])
        bots.append(bot)
        features_by_bot[bot.id] = cfg["features"]
        if cfg["config"]:
            config_ids.add(bot.id)
        if cfg.get("hub"):
            hub_ids.add(bot.id)
        any_transcribe = any_transcribe or (TRANSCRIBE in cfg["features"])
        await _setup_commands(bot, cfg["features"], cfg["config"], cfg.get("hub", False))
        await _setup_profile(bot, cfg["features"])
        logger.info("Бот %s (id=%s): функции=%s, config=%s, hub=%s",
                    cfg["name"], bot.id, cfg["features"], cfg["config"], cfg.get("hub", False))

    # Один диспетчер на всех ботов; маршрутизатор раздаёт функции по ботам/группам
    dp = Dispatcher()
    dp.message.middleware(ThrottleMiddleware())
    dp.message.middleware(RegisterUserMiddleware())
    routing = RoutingMiddleware(features_by_bot, config_ids, hub_ids)
    dp.message.middleware(routing)
    dp.callback_query.middleware(routing)
    dp.inline_query.middleware(routing)

    # Все роутеры подключаем один раз (маршрутизатор отфильтрует лишнее по каждому боту)
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(setconfig.router)
    dp.include_router(ai_chat.router)
    dp.include_router(transcribe.router)
    dp.include_router(payment.router)
    dp.include_router(inline.router)
    # Конвертер — раньше скачивания: его фильтр срабатывает только на «число+валюта»,
    # иначе сообщение уходит дальше в обработчик ссылок (F.text).
    dp.include_router(currency.router)
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
