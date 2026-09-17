import asyncio
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import (
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
    BotCommandScopeAllChatAdministrators, BotCommandScopeAllPrivateChats,
    ErrorEvent,
)
from bot.config import (
    BOT_TOKEN, TELEGRAM_LOCAL_API_URL, ADMIN_ID, WHISPER_PREWARM, ADMIN_TZ,
    REPORT_HOUR, HEALTHCHECK_EVERY_HOURS, ADMIN_LANG,
)
from bot.database import init_db, SessionLocal
from bot.database.repository import get_stats, add_traffic
from bot.utils import traffic, limits
from bot.features.common import alerts
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
from bot.features.common.admin import format_stats, platform_ranking
from bot.features.common.healthcheck import run_and_cache, format_health, format_alert
from bot.features.download.maintenance import clean_downloads, update_ytdlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# Приглушаем «болтовню»: aiogram пишет строку на КАЖДОЕ сообщение пользователя
# («Update … is handled»), а httpx — на каждый HTTP-запрос. В логе от этого тонут
# реальные ошибки, а файл растёт гигабайтами. Оставляем от них только предупреждения.
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

_ADMIN_ZONE = ZoneInfo(ADMIN_TZ)


def _seconds_until_report() -> float:
    """Сколько секунд до ближайшего REPORT_HOUR:00 по часовому поясу админа.
    Считаем от локального времени зоны на каждом витке, поэтому переход Молдовы
    на летнее/зимнее время учитывается автоматически — отчёт всегда в 12:00 по месту."""
    now = datetime.now(_ADMIN_ZONE)
    target = now.replace(hour=REPORT_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _seconds_until_slot(every_hours: int, skip_hour: int | None = None) -> float:
    """Сколько секунд до ближайшего будущего часа, кратного every_hours, на нулевой
    минуте по времени админа (при every_hours=2 — это 00:00, 02:00, 04:00 …). Сетка
    фиксированная и не зависит от момента запуска бота. skip_hour — час, который НЕ
    выбираем (например час суточного отчёта: его проверку делает сам отчёт, чтобы не
    было двойного прогона). Считаем от локального времени зоны, поэтому переход на
    летнее/зимнее время учитывается автоматически."""
    now = datetime.now(_ADMIN_ZONE)
    slot = now.replace(minute=0, second=0, microsecond=0)
    while slot <= now or slot.hour % every_hours != 0 or slot.hour == skip_hour:
        slot += timedelta(hours=1)
    return (slot - now).total_seconds()


def _make_bot(token: str) -> Bot:
    """Создаёт Bot; если задан локальный API-сервер — с ним (файлы до 2 ГБ)."""
    if TELEGRAM_LOCAL_API_URL:
        session = AiohttpSession(api=TelegramAPIServer.from_base(TELEGRAM_LOCAL_API_URL))
        return Bot(token=token, session=session)
    return Bot(token=token)


async def _setup_commands(bot: Bot):
    """Меню команд бота. /setconfig показываем админам групп и в личке (личные
    настройки), админ-команды — только в личном чате администратора."""
    common = [
        BotCommand(command="help", description="Справка по командам"),
        BotCommand(command="ai", description="Спросить ИИ"),
        BotCommand(command="premium", description="Купить Premium ✨"),
    ]
    await bot.set_my_commands(common, scope=BotCommandScopeDefault())
    # /setconfig в группах видят только их администраторы, а в личке — сам пользователь
    # (это его чат, там команда настраивает личные предпочтения).
    setconfig_cmd = BotCommand(command="setconfig", description="Настроить функции")
    await bot.set_my_commands(common + [setconfig_cmd],
                              scope=BotCommandScopeAllChatAdministrators())
    await bot.set_my_commands(common + [setconfig_cmd],
                              scope=BotCommandScopeAllPrivateChats())
    if ADMIN_ID:
        admin_cmds = common + [
            BotCommand(command="statistics", description="Статистика"),
            BotCommand(command="cleancache", description="Очистить кэш"),
            BotCommand(command="test", description="Проверить систему"),
        ]
        # Личный чат админа с ботом может ещё не существовать (админ не писал боту) —
        # тогда Telegram вернёт «chat not found». Не роняем из-за этого запуск.
        try:
            await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
        except Exception:
            logger.info("Не задал админ-команды (админ ещё не писал боту)")


async def _setup_profile(bot: Bot):
    """Ставит короткое описание (About) и описание (экран до Start) по языкам ru/uk/en,
    плюс английский по умолчанию для остальных языков."""
    for lang in ("ru", "uk", "en"):
        try:
            await bot.set_my_short_description(short_description=t("about", lang), language_code=lang)
            await bot.set_my_description(description=t("desc", lang), language_code=lang)
        except Exception:
            logger.exception("Не задал профиль (%s)", lang)
    try:  # дефолт для остальных языков — английский
        await bot.set_my_short_description(short_description=t("about", "en"))
        await bot.set_my_description(description=t("desc", "en"))
    except Exception:
        logger.exception("Не задал дефолтный профиль")


async def _restart_for_ytdlp(bot: Bot, old: str, new: str):
    """Перезапускает процесс, чтобы обновлённый yt-dlp начал работать.

    Docker с restart: unless-stopped поднимет ТОТ ЖЕ контейнер, поэтому установленная
    версия сохранится (пересборка образа её бы затёрла). Сначала дожидаемся конца
    текущих скачиваний: обрывать их посреди работы нельзя.
    """
    from bot.features.download.link import ACTIVE_DOWNLOADS
    for _ in range(30):                 # ждём до 15 минут, дальше уходим в любом случае
        if not ACTIVE_DOWNLOADS:
            break
        await asyncio.sleep(30)
    logger.info("Перезапуск ради обновления yt-dlp: %s -> %s", old, new)
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, f"♻️ Перезапускаюсь: yt-dlp {old} → {new}")
        except Exception:
            pass
    # Жёсткий выход намеренно: контейнер сейчас поднимется заново, а мягко погасить
    # polling из фоновой задачи надёжно не выходит.
    os._exit(0)


async def _daily_tasks(bot: Bot):
    """Каждый день в REPORT_HOUR:00 по времени админа: обновляем yt-dlp, прогоняем
    проверку функционала и шлём админу отчёт (статистика + результаты проверки).

    Если yt-dlp обновился — после отчёта перезапускаемся: иначе новая версия лежит
    установленной, но в работу не идёт, и обновление крутится вхолостую.
    """
    while True:
        await asyncio.sleep(_seconds_until_report())
        old, new = await asyncio.to_thread(update_ytdlp)
        if not ADMIN_ID:
            if old != new:
                await _restart_for_ytdlp(bot, old, new)
            continue
        try:
            async with SessionLocal() as session:
                stats = await get_stats(session)
            health = await run_and_cache()
            # Проверку выводим в том же порядке, что и «По платформам» (по использованию).
            # Версия yt-dlp отдельной строкой не нужна: свежесть проверяется внутри
            # самой проверки функционала («yt-dlp последний»).
            report = (f"{format_stats(stats, ADMIN_LANG)}\n\n"
                      f"{format_health(health, platform_ranking(stats), lang=ADMIN_LANG)}")
            await bot.send_message(ADMIN_ID, report, parse_mode="HTML")
        except Exception:
            logger.exception("Не удалось отправить дневной отчёт")
        if old != new:
            await _restart_for_ytdlp(bot, old, new)


async def _periodic_healthcheck(bot: Bot):
    """Раннее оповещение: гоняем проверку по ФИКСИРОВАННОЙ сетке часов (кратных
    HEALTHCHECK_EVERY_HOURS, по времени админа — при 2 это 00:00, 02:00, 04:00 …),
    независимо от момента запуска. Если что-то сломалось — сразу шлём админу короткую
    тревогу, всё ок — молчим (не спамим). Час суточного отчёта (REPORT_HOUR) пропускаем:
    его проверку делает _daily_tasks.

    Первый прогон — вскоре после старта, чтобы наполнить кэш для /statistics и сразу
    поймать поломку; дальше строго по слотам. Стартовый прогон идёт в любой час, в том
    числе в час отчёта: раньше он там пропускался (боялись столкнуться с _daily_tasks),
    и после перезапуска в этот час /statistics оставалась без проверки до следующего
    слота. Теперь сталкиваться нечем — run_and_cache пускает только один прогон разом,
    второй желающий просто ждёт результат первого."""
    # Короткая пауза, чтобы проверка не села на сам момент запуска: боту надо поднять
    # соединение с Telegram, а соседним контейнерам (POT-провайдер для YouTube) —
    # успеть начать отвечать. Раньше тут было 120 секунд, и всё это время /statistics
    # оставалась без проверки. Упавший пункт проверка и так перепроверяет ещё раз
    # через _RETRY_DELAY, поэтому не до конца прогревшийся сосед ложной тревоги не даст.
    await asyncio.sleep(15)
    startup = True
    while True:
        if not startup:
            # Следующий слот сетки, но НЕ час суточного отчёта — его проверку делает
            # _daily_tasks. Пропуск заложен прямо в расчёт слота (детерминированно),
            # иначе два прогона могли столкнуться в 12:00 (двойная нагрузка → ложные сбои).
            await asyncio.sleep(_seconds_until_slot(HEALTHCHECK_EVERY_HOURS, skip_hour=REPORT_HOUR))
        startup = False
        try:
            results = await run_and_cache()
            if ADMIN_ID:
                alert = format_alert(results, ADMIN_LANG)
                if alert:
                    await bot.send_message(ADMIN_ID, alert)
        except Exception:
            logger.exception("Периодическая проверка функционала упала")
        if HEALTHCHECK_EVERY_HOURS <= 0:
            return  # периодику выключили — но кэш для /statistics мы уже наполнили


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


async def _ensure_single_instance(bot: Bot):
    """Не даёт поднять ВТОРОГО бота на том же токене.

    Зачем: у одного токена может быть только один опрашивающий. Если их два (например,
    бот остался запущен на домашней машине, а его же подняли на сервере), Telegram
    отдаёт обновления то одному, то другому, а неподтверждённые доставляет повторно —
    и пользователи получают дубли ответов на одни и те же сообщения. Ошибка тихая:
    оба экземпляра при этом выглядят работающими.

    Как ловим: короткий getUpdates. Если кто-то уже опрашивает, Telegram отвечает
    ошибкой конфликта — тогда честно не стартуем, вместо того чтобы вступать в драку.
    Любая другая ошибка (сеть прилегла, Telegram недоступен) старт не блокирует: за
    временный сбой связи наказывать отказом запуска неправильно.
    """
    from aiogram.exceptions import TelegramConflictError
    try:
        await bot.get_updates(offset=-1, limit=1, timeout=1)
    except TelegramConflictError:
        raise SystemExit(
            "Этот бот уже где-то запущен на том же токене (Telegram: конфликт опроса). "
            "Два экземпляра на один токен дают дубли ответов, поэтому не стартую. "
            "Останови лишний — обычно это старый контейнер: docker compose stop bot")
    except Exception:
        logger.warning("Не удалось проверить единственность экземпляра — продолжаю",
                       exc_info=True)


async def main():
    await init_db()
    clean_downloads()  # чистим «хвосты» прошлых сессий

    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN в .env — запускать нечего.")

    bot = _make_bot(BOT_TOKEN)
    await _ensure_single_instance(bot)
    await _setup_commands(bot)
    await _setup_profile(bot)
    logger.info("Бот UnitySystem (id=%s) запущен со всеми функциями", bot.id)

    dp = Dispatcher()
    dp.message.middleware(ThrottleMiddleware())
    dp.message.middleware(RegisterUserMiddleware())
    routing = RoutingMiddleware()
    dp.message.middleware(routing)
    dp.callback_query.middleware(routing)
    dp.inline_query.middleware(routing)

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

    # Глобальный перехват необработанных ошибок: пишем в лог и коротко оповещаем админа
    # (не чаще раза в 10 минут, чтобы всплеск ошибок не превратился в спам).
    err_state = {"last": 0.0}

    async def _on_error(event: ErrorEvent):
        logger.error("Необработанная ошибка при обработке апдейта", exc_info=event.exception)
        if not ADMIN_ID:
            return
        now = asyncio.get_event_loop().time()
        if now - err_state["last"] < 600:
            return
        err_state["last"] = now
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ Ошибка в боте: {type(event.exception).__name__}: {event.exception}"[:400])
        except Exception:
            logger.exception("Не удалось отправить тревогу об ошибке")

    dp.errors.register(_on_error)

    # Уведомлять админа о КАЖДОМ сбое, показанном пользователю (через общую точку
    # limits.friendly_error). Контекст (ссылку) выставляет обработчик ссылок.
    alerts.configure(bot, ADMIN_ID)
    limits.set_failure_hook(alerts.note_failure)

    asyncio.create_task(_daily_tasks(bot))
    asyncio.create_task(_periodic_healthcheck(bot))
    asyncio.create_task(_flush_traffic())
    if WHISPER_PREWARM:
        from bot.features.transcribe.transcriber import warmup
        asyncio.create_task(asyncio.to_thread(warmup))

    print("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
