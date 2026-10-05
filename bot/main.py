import asyncio
import concurrent.futures
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import (
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
    BotCommandScopeAllChatAdministrators, BotCommandScopeAllPrivateChats,
    ErrorEvent, FSInputFile, InputMediaDocument,
)
from bot.config import (
    BOT_TOKEN, TELEGRAM_LOCAL_API_URL, ADMIN_ID, WHISPER_PREWARM, ADMIN_TZ,
    REPORT_HOUR, HEALTHCHECK_EVERY_HOURS, ADMIN_LANG, CRYPTOPAY_POLL_SECONDS,
    THREAD_POOL_SIZE, DEPS_CHECK_DAY, DEPS_CHECK_HOUR,
)
from bot.database import init_db, SessionLocal
from bot.database.repository import get_stats, add_traffic
from bot.utils import traffic, limits, heartbeat, secrets_filter
from bot.utils import home_tunnel  # ВРЕМЕННО home_tunnel
from bot.features.common import alerts
from bot.utils.i18n import t
from bot.middlewares.register_user import RegisterUserMiddleware
from bot.middlewares.throttle import ThrottleMiddleware, CallbackThrottleMiddleware
from bot.middlewares.routing import RoutingMiddleware
from bot.middlewares.retry import RetryAfterMiddleware, sends_file
from bot.features.common import start, admin, payment, inline
from bot.features.download import link
from bot.features.transcribe import transcribe
from bot.features.currency import convert as currency
from bot.features.ai import chat as ai_chat
from bot.features.config import setconfig
from bot.features.common.admin import format_stats, platform_ranking
from bot.features.common.healthcheck import run_and_cache, format_health, format_alert
from bot.features.common.backup import dump_database, dump_secrets
from bot.features.download.maintenance import clean_downloads, update_ytdlp

logging.basicConfig(level=logging.INFO)
# Маскировка секретов в логах. Ставим СРАЗУ после настройки логирования, до первых
# сообщений: токен бота стоит в адресах Bot API, ключ Gemini — в строке запроса, и
# любой упавший запрос печатал бы их целиком (см. bot/utils/secrets_filter.py).
secrets_filter.install()
logger = logging.getLogger(__name__)

# Живые фоновые задачи. asyncio держит на задачу только СЛАБУЮ ссылку: если сильной
# нигде нет, сборщик мусора вправе выбросить задачу прямо посреди ожидания — пульс,
# суточный отчёт или опрос платежей просто перестанут существовать, молча.
_BACKGROUND: set = set()


def _background(coro):
    """Запускает фоновую задачу и ДЕРЖИТ на неё ссылку до самого конца."""
    task = asyncio.ensure_future(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task
# Приглушаем «болтовню»: aiogram пишет строку на КАЖДОЕ сообщение пользователя
# («Update … is handled»), а httpx — на каждый HTTP-запрос. В логе от этого тонут
# реальные ошибки, а файл растёт гигабайтами. Оставляем от них только предупреждения.
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

_ADMIN_ZONE = ZoneInfo(ADMIN_TZ)


def _until(target: datetime, now: datetime) -> float:
    """Секунд от now до target – с учётом перевода часов.

    Оба времени в поясе админа, и Python при вычитании таких времён пояс ИГНОРИРУЕТ:
    считает по циферблату. В Молдове часы переводят дважды в год, и если перевод
    попадал между «сейчас» и целью, ожидание выходило на час короче или длиннее –
    отчёт, проверки и сверка библиотек срабатывали в 11:00 или 13:00. Поэтому считаем
    через UTC, где переводов нет.
    """
    return (target.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()


def _seconds_until_report() -> float:
    """Сколько секунд до ближайшего REPORT_HOUR:00 по часовому поясу админа.
    Считаем от локального времени зоны на каждом витке, поэтому переход Молдовы
    на летнее/зимнее время учитывается автоматически — отчёт всегда в 12:00 по месту."""
    now = datetime.now(_ADMIN_ZONE)
    target = now.replace(hour=REPORT_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return _until(target, now)


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
    return _until(slot, now)


# Сколько ждать ответа на отправку файла. Обычный срок aiogram – 60 секунд, а фильм
# на пару гигабайт сервер Bot API заливает в Telegram минутами: 26.09.2026 «Лучшее
# предложение» (HDRezka, 720p) падало с «Request timeout error» ровно на отправке.
_FILE_SEND_TIMEOUT = 30 * 60


class _Session(AiohttpSession):
    """Сессия, у которой отправка файла ждёт ответа дольше остальных запросов."""

    async def make_request(self, bot, method, timeout=None):
        if timeout is None and sends_file(method):
            timeout = _FILE_SEND_TIMEOUT
        return await super().make_request(bot, method, timeout)


def _make_bot(token: str) -> Bot:
    """Создаёт Bot; если задан локальный API-сервер — с ним (файлы до 2 ГБ).

    На сессию вешаем повтор запросов: Telegram при всплеске отправок отвечает «подожди
    столько-то секунд», и без повтора часть файлов просто не доходит (см. retry.py).
    """
    if TELEGRAM_LOCAL_API_URL:
        session = _Session(api=TelegramAPIServer.from_base(TELEGRAM_LOCAL_API_URL))
    else:
        session = _Session()
    session.middleware(RetryAfterMiddleware())
    return Bot(token=token, session=session)


async def _setup_commands(bot: Bot):
    """Меню команд бота. /setconfig показываем админам групп и в личке (личные
    настройки), админ-команды — только в личном чате администратора."""
    common = [
        BotCommand(command="help", description="Справка по командам"),
        BotCommand(command="ai", description="Спросить ИИ"),
        BotCommand(command="premium", description="Купить Premium ✨"),
    ]
    # /setconfig в группах видят только их администраторы, а в личке — сам пользователь
    # (это его чат, там команда настраивает личные предпочтения).
    setconfig_cmd = BotCommand(command="setconfig", description="Настроить функции")
    scopes = (
        (common, BotCommandScopeDefault()),
        (common + [setconfig_cmd], BotCommandScopeAllChatAdministrators()),
        (common + [setconfig_cmd], BotCommandScopeAllPrivateChats()),
    )
    # Меню команд — вещь КОСМЕТИЧЕСКАЯ, и падать из-за него нельзя. 23.09.2026 бот
    # попал в перезапуски, на каждом старте переставлял команды, и Telegram включил
    # ограничение частоты: «Retry in 456 seconds». Ошибка уходила наверх и роняла
    # запуск — то есть бот не работал вообще из-за надписи в меню. Теперь список
    # команд не выставился — просто останется прежним, а бот поднимется.
    for cmds, scope in scopes:
        try:
            await bot.set_my_commands(cmds, scope=scope)
        except Exception as e:
            logger.warning("Не обновил меню команд (%s): %s",
                           type(scope).__name__, e)
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
    # Одной строкой, без трассировки: типовая причина здесь — то же ограничение
    # частоты у Telegram, что и у меню команд. Полная простыня стека в логе на каждый
    # язык только мешает искать настоящие ошибки.
    for lang in ("ru", "uk", "en"):
        try:
            await bot.set_my_short_description(short_description=t("about", lang), language_code=lang)
            await bot.set_my_description(description=t("desc", lang), language_code=lang)
        except Exception as e:
            logger.warning("Не обновил профиль (%s): %s", lang, e)
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
    from bot.features.download.job import ACTIVE_DOWNLOADS
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
            # Проверку выводим в том же порядке, что и «По платформам» (по использованию).
            # Версия yt-dlp отдельной строкой не нужна: свежесть проверяется внутри
            # самой проверки функционала («yt-dlp последний»).
            report = format_stats(stats, ADMIN_LANG)
            if not await home_tunnel.check_now():   # ВРЕМЕННО home_tunnel: без дома – отчёт без проверки
                health = await run_and_cache()
                report += f"\n\n{format_health(health, platform_ranking(stats), lang=ADMIN_LANG)}"
            await _send_report(bot, report)
        except Exception:
            logger.exception("Не удалось отправить дневной отчёт")
        if old != new:
            await _restart_for_ytdlp(bot, old, new)


async def _send_report(bot: Bot, report: str):
    """Отправляет суточный отчёт, а под ним – копию базы и архив секретов.

    Отчёт – обычным сообщением: у подписи к файлу предел 1024 символа, а отчёт с
    сорока пунктами проверки почти всегда длиннее, так что на деле он и раньше
    приходил отдельно. Под ним – оба файла ОДНИМ альбомом: копия базы
    (unitysystem-ГГГГ-ММ-ДД.db) и архив секретов (unitysystem-secrets.zip: .env, куки,
    сессия посредника, ключи WireGuard) – с одной базой бота заново не поднять. Архив
    открытый, как и копия базы, – так решил владелец; кто получит доступ к этому чату,
    получит и всё, что в архиве.

    Отчёт важнее копий: он уходит первым, и что бы ни случилось с файлами – не снялись,
    не ушли, отвергнуты Telegram, – отчёт уже доставлен.
    """
    files = []
    try:
        await bot.send_message(ADMIN_ID, report, parse_mode="HTML")
        db = await dump_database()
        if db:
            files.append(db)
        secrets, _ = await asyncio.to_thread(dump_secrets)
        if secrets:
            files.append(secrets)
        if len(files) == 1:
            await bot.send_document(ADMIN_ID, FSInputFile(files[0]))
        elif files:
            await bot.send_media_group(
                ADMIN_ID, [InputMediaDocument(media=FSInputFile(p)) for p in files])
    except Exception:
        logger.exception("Не удалось отправить дневной отчёт или копии")
    finally:
        for path in files:
            try:
                os.remove(path)
            except OSError:
                pass


async def _periodic_healthcheck(bot: Bot):
    """Раннее оповещение: гоняем проверку по ФИКСИРОВАННОЙ сетке часов (кратных
    HEALTHCHECK_EVERY_HOURS, по времени админа — при 2 это 00:00, 02:00, 04:00 …),
    независимо от момента запуска. Если что-то сломалось — сразу шлём админу короткую
    тревогу, всё ок — молчим (не спамим). Час суточного отчёта (REPORT_HOUR) пропускаем:
    его проверку делает _daily_tasks.

    Первый прогон — вскоре после старта, чтобы наполнить кэш для /statistics и сразу
    поймать поломку; по его итогам уходит ПОЛНЫЙ отчёт (как суточный), потому что
    перезапуск — это почти всегда деплой и надо видеть, что доехало целым. Дальше
    строго по слотам и снова только тревогами. Стартовый прогон идёт в любой час, в том
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
        first = startup      # снимаем флаг ДО прогона: упади он с ошибкой, следующий
        startup = False      # виток всё равно должен уйти спать до слота, а не крутиться
        if HEALTHCHECK_EVERY_HOURS > 0 and await home_tunnel.check_now():  # ВРЕМЕННО home_tunnel: без дома – ни проверки, ни тревог
            logger.info("Домашнего туннеля нет – проверку площадок пропускаю")
            continue
        try:
            results = await run_and_cache()
            if ADMIN_ID:
                if first:
                    # После перезапуска шлём ПОЛНЫЙ отчёт, а не тревогу: перезапуск —
                    # это почти всегда деплой, и первое, что нужно знать, — доехала ли
                    # сборка целой. Тревога отвечала только «что сломалось», и после
                    # удачного деплоя бот молчал: не отличить «всё хорошо» от «отчёт
                    # не дошёл».
                    async with SessionLocal() as session:
                        stats = await get_stats(session)
                    report = (f"{format_stats(stats, ADMIN_LANG)}\n\n"
                              f"{format_health(results, platform_ranking(stats), lang=ADMIN_LANG)}")
                    await bot.send_message(ADMIN_ID, report, parse_mode="HTML")
                else:
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


async def _watch_crypto_invoices(bot: Bot):
    """Следит за оплатой крипто-счетов и включает премиум.

    Опросом, а не вебхуком: вебхук требует публичного HTTPS-адреса с сертификатом,
    а бот живёт на опросе Telegram, домена у него нет. Спрашиваем только про СВОИ
    неоплаченные счета, так что запрос лёгкий даже раз в 15 секунд.

    Почему выдача премиума живёт здесь, а не в обработчике кнопки: человек может
    закрыть бота сразу после оплаты, а мы — перезапуститься в этот самый момент.
    Счёт лежит в базе, и его всё равно доведут до конца.
    """
    from bot.database.repository import open_crypto_invoices, close_crypto_invoice
    from bot.features.common import cryptopay
    from bot.features.common.payment import grant_crypto

    if not cryptopay.available():
        return                       # токена нет — способ оплаты выключен целиком
    while True:
        await asyncio.sleep(CRYPTOPAY_POLL_SECONDS)
        try:
            async with SessionLocal() as session:
                pending = await open_crypto_invoices(session)
            if not pending:
                continue
            owners = dict(pending)
            invoices = await asyncio.to_thread(cryptopay.get_invoices, list(owners))
            for inv in invoices:
                # Каждый счёт — в своей попытке. Иначе один странный счёт (скажем, с
                # нечисловым payload) роняет весь проход, повторяется каждые 15 секунд,
                # и до остальных счетов очередь не доходит НИКОГДА: человек заплатил,
                # а премиум не включается из-за чужого платежа.
                try:
                    status = inv.get("status")
                    if status not in ("paid", "expired"):
                        continue
                    invoice_id = inv["invoice_id"]
                    # Кому включать премиум, берём из payload самого счёта: он пришёл
                    # от сервиса вместе с оплатой и не зависит от нашей записи. Своя
                    # запись — запасной путь, если payload когда-нибудь потеряется.
                    try:
                        user_id = int(inv.get("payload") or 0)
                    except (TypeError, ValueError):
                        user_id = 0
                    user_id = user_id or owners.get(invoice_id, 0)
                    if status == "paid" and user_id:
                        # Счёт выставлен в долларах, поэтому amount — это и есть
                        # доллары, чем бы человек ни заплатил.
                        await grant_crypto(bot, user_id, invoice_id,
                                           float(inv.get("amount") or 0))
                    async with SessionLocal() as session:
                        await close_crypto_invoice(session, invoice_id, status)
                except Exception:
                    logger.exception("Крипто-счёт %s обработать не вышло",
                                     inv.get("invoice_id"))
        except Exception:
            logger.exception("Опрос крипто-счетов сорвался")


def _seconds_until_month_day(day: int, hour: int) -> float:
    """Сколько секунд до ближайшего day-го числа месяца, hour:00 по времени админа.
    Число больше длины месяца (31 в феврале) – последний день месяца."""
    import calendar
    now = datetime.now(_ADMIN_ZONE)
    year, month = now.year, now.month
    while True:
        last = calendar.monthrange(year, month)[1]
        target = now.replace(year=year, month=month, day=min(day, last), hour=hour,
                             minute=0, second=0, microsecond=0)
        if target > now:
            return _until(target, now)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


async def _monthly_deps_check(bot: Bot):
    """Раз в месяц сверяет библиотеки бота с PyPI и шлёт админу итог (см. deps_check).
    Ничего не обновляет: новая версия библиотеки может поменять поведение бота."""
    from bot.features.common import deps_check

    if not ADMIN_ID or DEPS_CHECK_DAY <= 0:
        return
    while True:
        await asyncio.sleep(_seconds_until_month_day(DEPS_CHECK_DAY, DEPS_CHECK_HOUR))
        try:
            res = await asyncio.to_thread(deps_check.check)
            text = deps_check.format_report(res, ADMIN_LANG)
            # Длинный список (много уязвимостей) режем по строкам: у Telegram 4096
            # символов на сообщение.
            chunk = ""
            for line in text.split("\n"):
                if len(chunk) + len(line) + 1 > 4000:
                    await bot.send_message(ADMIN_ID, chunk)
                    chunk = ""
                chunk += line + "\n"
            if chunk.strip():
                await bot.send_message(ADMIN_ID, chunk)
        except Exception:
            logger.exception("Ежемесячная сверка библиотек не удалась")
        # Страховка от двойного срабатывания в ту же секунду: до следующего раза – месяц.
        await asyncio.sleep(60)


async def _warm_hdrezka():
    """Держит пропуск HDRezka свежим, чтобы за него не платил тот, кто пришёл первым.

    Анти-бот-головоломку решает браузер на общем потоке Playwright, где стоят ещё
    карточки X и фото Instagram. Сама она занимает пару секунд, но в час общей
    проверки очередь на этом потоке растягивает её до десятков секунд: 21.09.2026
    «HDRezka сериал» занял 42.6с вместо обычных 1.6с — не потому, что сайт лёг, а
    потому что ждал очереди. Обновляем заранее и в тишине.
    """
    from bot.features.download.downloaders import hdrezka_gate

    # Первый прогон — не в момент старта: на старте и так поднимается всё сразу, а
    # проверка функционала всё равно сходит на сайт сама.
    await asyncio.sleep(90)
    while True:
        await asyncio.to_thread(hdrezka_gate.warm)
        await asyncio.sleep(hdrezka_gate.warm_interval())


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
    dp.callback_query.middleware(CallbackThrottleMiddleware())
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
                # Текст исключения может содержать адрес запроса с ключом —
                # маскируем перед отправкой, лог уже прикрыт фильтром.
                secrets_filter.mask(
                    f"⚠️ Ошибка в боте: {type(event.exception).__name__}: "
                    f"{event.exception}")[:400])
        except Exception:
            logger.exception("Не удалось отправить тревогу об ошибке")

    dp.errors.register(_on_error)

    # Уведомлять админа о КАЖДОМ сбое, показанном пользователю (через общую точку
    # limits.friendly_error). Контекст (ссылку) выставляет обработчик ссылок.
    alerts.configure(bot, ADMIN_ID)
    limits.set_failure_hook(alerts.note_failure)

    # Пульс: задача отмечает, что основной цикл жив, а сторож в отдельном потоке
    # перезапускает процесс, если отметки перестали появляться (см. heartbeat.py).
    # Ссылки на задачи ДЕРЖИМ. asyncio хранит только слабую ссылку на задачу: пока
    # она чего-то ждёт, её вправе собрать сборщик мусора — и фоновая работа тихо
    # исчезнет, без ошибки и без следа. Набор живёт до конца процесса, а задача
    # убирает себя из него сама, когда закончится.
    # Свой пул потоков под синхронную работу. Стандартный — «ядра + 4» (на сервере 8),
    # а только загрузок одновременно бывает до восьми: в такой момент мгновенные вещи
    # (курс валют, оплата, расшифровка) ждали свободного потока наравне с фильмом.
    asyncio.get_running_loop().set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=THREAD_POOL_SIZE,
                                              thread_name_prefix="work"))
    logger.info("Пул рабочих потоков: %d", THREAD_POOL_SIZE)

    # yt-dlp грузит свои плагины при первом YoutubeDL(). Если первые экземпляры
    # создаются разом из разных потоков (старт проверки площадок), загрузка плагинов
    # гоняется сама с собой и в процессе пропадают извлекатели: «No suitable extractor».
    # Поэтому первый экземпляр делаем здесь, один раз и до любой фоновой работы.
    try:
        from bot.features.download.downloaders.ytdlp_wrapper import BASE_OPTS
        import yt_dlp
        yt_dlp.YoutubeDL({**BASE_OPTS, "quiet": True}).close()
    except Exception:
        logger.warning("Не удалось прогреть yt-dlp", exc_info=True)

    _background(heartbeat.beat())
    heartbeat.start_watchdog()

    _background(_daily_tasks(bot))
    _background(_periodic_healthcheck(bot))
    _background(home_tunnel.watch())  # ВРЕМЕННО home_tunnel
    _background(_flush_traffic())
    _background(_warm_hdrezka())
    _background(_monthly_deps_check(bot))
    _background(_watch_crypto_invoices(bot))
    if WHISPER_PREWARM:
        from bot.features.transcribe.transcriber import warmup
        _background(asyncio.to_thread(warmup))

    print("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
