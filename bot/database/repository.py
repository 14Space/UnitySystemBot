import hashlib
import json
import logging
import statistics
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func, delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from bot.database.models import (
    User, CachedFile, DownloadStat, MonthlyTraffic, ChatSettings, CheckTiming, Payment,
    StashedLink, AiThread, AiUsage, CryptoInvoice,
)
from bot.utils.platform_detector import normalize_cache_url

logger = logging.getLogger(__name__)

# id бота, обрабатывающего текущий апдейт. Выставляет RoutingMiddleware на каждый
# апдейт. file_id в Telegram привязан к отправившему боту, поэтому кэш ведём отдельно
# по каждому боту — и в ключе (url_hash), и колонкой bot_id.
current_bot_id: ContextVar[int | None] = ContextVar("current_bot_id", default=None)


def _cache_hash(url: str, quality, bot_id: int | None) -> str:
    """Ключ кэша. Ссылку приводим к каноничному виду прямо здесь — так одна и та же вещь,
    присланная с разными хвостами (?si=, ?stkn=, метка времени, зеркало), попадает в одну
    запись, и это работает сразу во всех местах, где кэш читается или пишется."""
    return hashlib.md5(
        f"{bot_id}:{normalize_cache_url(url)}:{quality}".encode()).hexdigest()


# Как часто обновляем отметку активности. Чаще незачем: человек, приславший десять
# ссылок подряд, — это один активный человек, а запись в базу на каждое сообщение
# ничего не уточняет и только греет диск.
_SEEN_EVERY = timedelta(minutes=30)


async def get_or_create_user(
    session: AsyncSession, user_id: int, username: str, language: str = "ru"
) -> User:
    """Возвращает пользователя из БД, или создаёт нового. Заодно отмечает активность."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    result = await session.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(user_id=user_id, username=username, language=language, last_seen=now)
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            # Гонка: пользователя уже создал параллельный запрос — берём его
            await session.rollback()
            result = await session.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one()
        return user

    if user.last_seen is None or now - user.last_seen > _SEEN_EVERY:
        user.last_seen = now
        await session.commit()
    return user


async def is_premium(session: AsyncSession, user_id: int) -> bool:
    """Есть ли у пользователя Premium"""
    result = await session.execute(select(User.is_premium).where(User.user_id == user_id))
    return bool(result.scalar_one_or_none())


async def set_premium(session: AsyncSession, user_id: int, value: bool = True) -> None:
    """Включает/выключает Premium у пользователя.

    Если записи о человеке ещё нет — СОЗДАЁМ её. Раньше функция в этом случае молча
    ничего не делала, и это стоило бы денег: в базу попадают только те, кто присылал
    боту запрос (см. RegisterUserMiddleware), а нажатие 🔒 — это callback, запросом он
    не считается. То есть участник группы мог нажать замок, оплатить счёт и не получить
    ничего, причём тихо: ни ошибки, ни следа в логах.
    """
    result = await session.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        logger.warning("Premium для %s: записи о человеке не было, создаю", user_id)
        user = User(user_id=user_id, username="", language="ru")
        session.add(user)
    user.is_premium = value
    try:
        await session.commit()
    except IntegrityError:
        # Гонка: запись создал параллельный запрос — берём её и дожимаем премиум.
        await session.rollback()
        user = (await session.execute(
            select(User).where(User.user_id == user_id))).scalar_one()
        user.is_premium = value
        await session.commit()


async def add_payment(session: AsyncSession, user_id: int, charge_id: str,
                      stars: int, method: str = "stars", usd: float | None = None) -> None:
    """Записывает покупку. charge_id — номер платежа у Telegram: без него возврат
    звёзд сделать нельзя, Telegram требует именно его.

    Повторную запись того же charge_id молча пропускаем: Telegram может доставить
    одно и то же сообщение об оплате дважды (если наш ответ не дошёл), и от этого
    в истории не должно появляться двух покупок.
    """
    session.add(Payment(user_id=user_id, charge_id=charge_id, stars=stars,
                        method=method, usd=usd))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()


async def refund_payment(session: AsyncSession, charge_id: str) -> int | None:
    """Помечает платёж возвращённым и возвращает id покупателя (или None, если такого
    платежа у нас нет). Премиум снимает вызывающий код."""
    row = (await session.execute(
        select(Payment).where(Payment.charge_id == charge_id))).scalar_one_or_none()
    if row is None:
        return None
    row.refunded = True
    await session.commit()
    return row.user_id


async def get_payment_summary(session: AsyncSession) -> dict:
    """Сводка по покупкам: сколько оплачено, сколько возвращено, звёзды и доллары."""
    paid = (await session.execute(
        select(func.count(Payment.id),
               func.coalesce(func.sum(Payment.stars), 0),
               func.coalesce(func.sum(Payment.usd), 0.0))
        .where(Payment.refunded.is_(False)))).one()
    refunded = (await session.execute(
        select(func.count(Payment.id)).where(Payment.refunded.is_(True)))).scalar() or 0
    return {"count": paid[0] or 0, "stars": paid[1] or 0,
            "usd": float(paid[2] or 0), "refunded": refunded}


async def user_language(session: AsyncSession, user_id: int) -> str:
    """Язык человека из базы. Нужен там, где сообщения шлёт ФОНОВАЯ задача: объекта
    пользователя у неё нет, а писать «поздравляем с покупкой» не на его языке — плохо."""
    lang = (await session.execute(
        select(User.language).where(User.user_id == user_id))).scalar_one_or_none()
    return lang or "ru"


async def add_crypto_invoice(session: AsyncSession, invoice_id: int, user_id: int) -> None:
    """Запоминает выставленный счёт, чтобы потом спросить про его оплату."""
    session.add(CryptoInvoice(invoice_id=invoice_id, user_id=user_id))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()


async def active_crypto_invoice(session: AsyncSession, user_id: int) -> int | None:
    """Номер незакрытого счёта этого человека, если он есть.

    Нужен, чтобы не плодить счета: каждое нажатие «Оплатить криптой» выставляло новый,
    а опрос смотрит только сотню последних — спамом кнопки можно было вытеснить из
    опроса ЧУЖОЙ настоящий счёт, и человек остался бы без премиума после оплаты.
    """
    return (await session.execute(
        select(CryptoInvoice.invoice_id)
        .where(CryptoInvoice.user_id == user_id, CryptoInvoice.status == "active")
        .order_by(CryptoInvoice.invoice_id.desc()).limit(1))).scalar_one_or_none()


async def open_crypto_invoices(session: AsyncSession, limit: int = 100) -> list[tuple[int, int]]:
    """Наши счета, которые ещё ждут оплаты: [(номер счёта, покупатель)].

    Предел нужен, потому что спрашиваем про них одним запросом: Crypto Pay берёт до
    1000 счетов за раз, а нам хватает и сотни самых свежих.
    """
    rows = (await session.execute(
        select(CryptoInvoice.invoice_id, CryptoInvoice.user_id)
        .where(CryptoInvoice.status == "active")
        .order_by(CryptoInvoice.invoice_id.desc()).limit(limit))).all()
    return [(r[0], r[1]) for r in rows]


async def close_crypto_invoice(session: AsyncSession, invoice_id: int, status: str) -> None:
    """Счёт больше не ждём: оплачен или протух."""
    row = (await session.execute(
        select(CryptoInvoice).where(CryptoInvoice.invoice_id == invoice_id))).scalar_one_or_none()
    if row is None:
        return
    row.status = status
    await session.commit()


async def increment_download(session: AsyncSession, platform: str) -> None:
    """Увеличивает счётчик запросов для платформы.

    Прибавляем ОДНИМ запросом к базе (UPDATE … count + 1), а не «прочитал, сложил,
    записал»: два запроса одной площадки в одну секунду читали одно и то же значение
    и записывали одно и то же — один запрос просто терялся. А когда счётчика ещё нет,
    вставка от двух сразу падала с IntegrityError.
    """
    updated = (await session.execute(
        update(DownloadStat).where(DownloadStat.platform == platform)
        .values(count=DownloadStat.count + 1))).rowcount
    if not updated:
        session.add(DownloadStat(platform=platform, count=1))
        try:
            await session.commit()
        except IntegrityError:      # успел параллельный запрос — просто прибавим
            await session.rollback()
            await session.execute(
                update(DownloadStat).where(DownloadStat.platform == platform)
                .values(count=DownloadStat.count + 1))
            await session.commit()
        return
    await session.commit()


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def add_traffic(session: AsyncSession, nbytes: int, nfiles: int) -> None:
    """Прибавляет скачанные байты/файлы к счётчику текущего месяца (для износа SSD)."""
    if nbytes <= 0:
        return
    month = _current_month()
    row = (await session.execute(
        select(MonthlyTraffic).where(MonthlyTraffic.month == month)
    )).scalar_one_or_none()
    if row:
        row.total_bytes += nbytes
        row.files += nfiles
    else:
        session.add(MonthlyTraffic(month=month, total_bytes=nbytes, files=nfiles))
    await session.commit()


async def get_traffic(session: AsyncSession, limit: int = 6) -> list[dict]:
    """Трафик по месяцам (свежие сверху): [{'month','bytes','files'}]."""
    rows = (await session.execute(
        select(MonthlyTraffic).order_by(MonthlyTraffic.month.desc()).limit(limit)
    )).scalars().all()
    return [{"month": r.month, "bytes": r.total_bytes, "files": r.files} for r in rows]


async def get_stats(session: AsyncSession) -> dict:
    """Сводка для админа: скачивания по платформам, число юзеров, языки, трафик"""
    rows = (await session.execute(select(DownloadStat))).scalars().all()
    downloads = {r.platform: r.count for r in rows}

    users = (await session.execute(select(func.count(User.id)))).scalar() or 0
    premium = (await session.execute(
        select(func.count(User.id)).where(User.is_premium.is_(True)))).scalar() or 0
    # Активные — те, кто обращался к боту за последний месяц. У старых записей отметки
    # нет вовсе (её завели позже), и они сюда не попадают — это честно: когда они
    # пользовались ботом в последний раз, мы не знаем.
    month_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    active = (await session.execute(
        select(func.count(User.id)).where(User.last_seen >= month_ago))).scalar() or 0

    lang_rows = (await session.execute(
        select(User.language, func.count(User.id)).group_by(User.language)
    )).all()
    languages = {(lang or "?"): cnt for lang, cnt in lang_rows}

    return {
        "downloads": downloads,
        "total_downloads": sum(downloads.values()),
        "users": users,
        "active": active,
        "premium": premium,
        "languages": languages,
        "payments": await get_payment_summary(session),
        "traffic": await get_traffic(session),
    }


# Выключенные функции читаются НА КАЖДОЕ сообщение (см. RoutingMiddleware), а меняются
# раз в месяц через /setconfig. Поэтому держим их в памяти: это снимает один запрос к
# базе с каждого сообщения в каждом чате. Кэш живёт минуту и сбрасывается сразу при
# изменении настройки, так что «выключил функцию — она сразу выключилась» сохраняется.
_DISABLED_CACHE: dict[int, tuple[float, set[str]]] = {}
_DISABLED_TTL = 60.0


def forget_chat_settings(chat_id: int | None = None) -> None:
    """Сбрасывает кэш настроек чата (или весь, если чат не указан)."""
    if chat_id is None:
        _DISABLED_CACHE.clear()
    else:
        _DISABLED_CACHE.pop(chat_id, None)


async def get_disabled_features(session: AsyncSession, chat_id: int) -> set[str]:
    """Множество выключенных в этом чате функций (пусто = всё включено)."""
    hit = _DISABLED_CACHE.get(chat_id)
    if hit and (time.monotonic() - hit[0]) < _DISABLED_TTL:
        return set(hit[1])
    row = (await session.execute(
        select(ChatSettings).where(ChatSettings.chat_id == chat_id)
    )).scalar_one_or_none()
    disabled = ({f for f in row.disabled_features.split(",") if f}
                if row and row.disabled_features else set())
    _DISABLED_CACHE[chat_id] = (time.monotonic(), set(disabled))
    # Чатов у бота немного, но память не бесконечна: держим самые свежие.
    if len(_DISABLED_CACHE) > 512:
        oldest = sorted(_DISABLED_CACHE.items(), key=lambda kv: kv[1][0])[:128]
        for key, _ in oldest:
            _DISABLED_CACHE.pop(key, None)
    return disabled


async def set_feature(session: AsyncSession, chat_id: int, feature: str, enable: bool) -> None:
    """Включает/выключает функцию в чате (для /setconfig)."""
    row = (await session.execute(
        select(ChatSettings).where(ChatSettings.chat_id == chat_id)
    )).scalar_one_or_none()
    if not row:
        row = ChatSettings(chat_id=chat_id, disabled_features="")
        session.add(row)
    disabled = {f for f in (row.disabled_features or "").split(",") if f}
    if enable:
        disabled.discard(feature)
    else:
        disabled.add(feature)
    row.disabled_features = ",".join(sorted(disabled))
    await session.commit()
    forget_chat_settings(chat_id)      # иначе тумблер сработал бы с задержкой


async def get_slideshow_mode(session: AsyncSession, chat_id: int, default: str = "video") -> str:
    """Режим слайдшоу TikTok в этом чате: video | photos | ask. Дефолт зависит от типа
    чата: в группах — video, в личке — ask (передаётся вызывающим кодом)."""
    row = (await session.execute(
        select(ChatSettings).where(ChatSettings.chat_id == chat_id)
    )).scalar_one_or_none()
    return row.slideshow_mode if row and row.slideshow_mode else default


async def set_slideshow_mode(session: AsyncSession, chat_id: int, mode: str) -> None:
    """Сохраняет режим слайдшоу для группы (для /setconfig)."""
    row = (await session.execute(
        select(ChatSettings).where(ChatSettings.chat_id == chat_id)
    )).scalar_one_or_none()
    if not row:
        row = ChatSettings(chat_id=chat_id, disabled_features="", slideshow_mode=mode)
        session.add(row)
    else:
        row.slideshow_mode = mode
    await session.commit()


DEFAULT_CURRENCY_TARGETS = ["USD", "EUR"]


async def get_currency_targets(session: AsyncSession, chat_id: int) -> list[str]:
    """Валюты для конвертации в этой группе. Нет строки — набор по умолчанию."""
    row = (await session.execute(
        select(ChatSettings).where(ChatSettings.chat_id == chat_id)
    )).scalar_one_or_none()
    if row is None or row.currency_targets is None:
        return list(DEFAULT_CURRENCY_TARGETS)
    return [c for c in row.currency_targets.split(",") if c]


async def toggle_currency_target(session: AsyncSession, chat_id: int, code: str) -> None:
    """Добавляет/убирает валюту из набора конвертации для группы."""
    row = (await session.execute(
        select(ChatSettings).where(ChatSettings.chat_id == chat_id)
    )).scalar_one_or_none()
    if row is None:
        # первая настройка — стартуем от набора по умолчанию
        current = list(DEFAULT_CURRENCY_TARGETS)
        row = ChatSettings(chat_id=chat_id, disabled_features="")
        session.add(row)
    elif row.currency_targets is None:
        current = list(DEFAULT_CURRENCY_TARGETS)
    else:
        current = [c for c in row.currency_targets.split(",") if c]
    if code in current:
        current.remove(code)
    else:
        current.append(code)
    row.currency_targets = ",".join(current)
    await session.commit()


async def get_audio_track(session: AsyncSession, chat_id: int) -> bool:
    """Слать ли музыку к фото-слайдшоу. По умолчанию ДА: если у чата ещё нет записи
    настроек — считаем включённым (безопасно, срабатывает только для фото-слайдшоу)."""
    row = (await session.execute(
        select(ChatSettings).where(ChatSettings.chat_id == chat_id)
    )).scalar_one_or_none()
    return True if row is None else bool(row.audio_track)


async def set_audio_track(session: AsyncSession, chat_id: int, on: bool) -> None:
    """Включает/выключает аудиодорожку к видео для группы."""
    row = (await session.execute(
        select(ChatSettings).where(ChatSettings.chat_id == chat_id)
    )).scalar_one_or_none()
    if row is None:
        row = ChatSettings(chat_id=chat_id, disabled_features="")
        session.add(row)
    row.audio_track = on
    await session.commit()


async def get_compress_shorts(session: AsyncSession, chat_id: int, default: bool) -> bool:
    """Брать ли короткие видео в качестве пониже (быстрее). Если чат ничего не менял
    (NULL) — возвращаем default: его задаёт вызывающий код по типу чата (в группах ВКЛ,
    в личке ВЫКЛ)."""
    row = (await session.execute(
        select(ChatSettings).where(ChatSettings.chat_id == chat_id)
    )).scalar_one_or_none()
    if row is None or row.compress_shorts is None:
        return default
    return bool(row.compress_shorts)


async def set_compress_shorts(session: AsyncSession, chat_id: int, on: bool) -> None:
    """Явно включает/выключает сжатие коротких видео в этом чате."""
    row = (await session.execute(
        select(ChatSettings).where(ChatSettings.chat_id == chat_id)
    )).scalar_one_or_none()
    if row is None:
        row = ChatSettings(chat_id=chat_id, disabled_features="")
        session.add(row)
    row.compress_shorts = on
    await session.commit()


async def get_cached_file_id(session: AsyncSession, url: str, quality: str = None) -> str | None:
    """Возвращает Telegram file_id для пары URL+качество (для текущего бота), если он в кэше"""
    bot_id = current_bot_id.get()
    url_hash = _cache_hash(url, quality, bot_id)
    result = await session.execute(select(CachedFile).where(CachedFile.url_hash == url_hash))
    cached = result.scalar_one_or_none()
    return cached.file_id if cached else None


async def get_any_cached_file(session: AsyncSession, url: str) -> dict | None:
    """Любой готовый файл этой ссылки для ТЕКУЩЕГО бота (для inline). Возвращает file_id и тип.
    Фильтруем по bot_id: чужой file_id всё равно невалиден (DOCUMENT_INVALID)."""
    bot_id = current_bot_id.get()
    result = await session.execute(
        select(CachedFile)
        .where(CachedFile.original_url == normalize_cache_url(url),
               CachedFile.bot_id == bot_id)
        .limit(1)
    )
    row = result.scalars().first()
    if not row:
        return None
    return {"file_id": row.file_id, "quality": row.quality or ""}


async def clear_cache(session: AsyncSession) -> int:
    """Удаляет кэш file_id ТЕКУЩЕГО бота. Возвращает число удалённых записей.
    Кэш бот-аварный (file_id привязан к отправившему боту), поэтому чистим только свои
    записи, чужих ботов не трогаем. Сами файлы на серверах Telegram не тронуты."""
    from sqlalchemy import delete
    bot_id = current_bot_id.get()
    cond = CachedFile.bot_id == bot_id
    count = (await session.execute(
        select(func.count(CachedFile.id)).where(cond))).scalar() or 0
    await session.execute(delete(CachedFile).where(cond))
    await session.commit()
    return count


async def save_cached_file_id(session: AsyncSession, url: str, file_id: str, quality: str = None) -> None:
    """Сохраняет file_id в кэш текущего бота (или обновляет, если запись уже есть)"""
    bot_id = current_bot_id.get()
    url_hash = _cache_hash(url, quality, bot_id)
    existing = await session.execute(select(CachedFile).where(CachedFile.url_hash == url_hash))
    row = existing.scalar_one_or_none()
    if row:
        row.file_id = file_id
        row.bot_id = bot_id
    else:
        session.add(CachedFile(url_hash=url_hash, original_url=normalize_cache_url(url),
                               file_id=file_id, quality=quality, bot_id=bot_id))
    await session.commit()


async def clear_cache_entry(session: AsyncSession, url: str, quality: str = None) -> None:
    """Удаляет ОДНУ запись кэша. Нужна проверке функционала: она пишет заведомо
    фальшивую запись, читает её и обязана убрать за собой, чтобы не копить мусор
    в боевой таблице."""
    url_hash = _cache_hash(url, quality, current_bot_id.get())
    row = (await session.execute(
        select(CachedFile).where(CachedFile.url_hash == url_hash))).scalar_one_or_none()
    if row:
        await session.delete(row)
        await session.commit()


# Сколько последних прогонов держим по каждой проверке. Норма считается по медиане
# этого окна: одиночный выброс (чужой сервер тормознул) её не сдвигает, а устойчивое
# замедление – сдвигает.
CHECK_HISTORY_KEEP = 20


async def save_link_stash(session: AsyncSession, key: str, url: str, chat_id: int,
                          user_msg_id: int, premium: bool = False,
                          kind: str = "quality", payload: dict | None = None) -> None:
    """Запоминает ссылку под экраном с кнопками — чтобы они пережили перезапуск бота.

    kind говорит, какой это экран (выбор качества, HDRezka, слайдшоу TikTok), payload
    хранит мелочи вида: сезон и серию, номер поста, кто именно нажал. Заодно подчищаем
    совсем старые записи: экран, которому больше недели, никто уже не нажмёт, а таблица
    расти без предела не должна.
    """
    session.add(StashedLink(id=key, url=url, chat_id=chat_id,
                            user_msg_id=user_msg_id, premium=premium, kind=kind,
                            payload=json.dumps(payload, ensure_ascii=False) if payload else None))
    await session.execute(
        delete(StashedLink).where(
            StashedLink.at < datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()


async def load_link_stash(session: AsyncSession, key: str) -> dict | None:
    """Достаёт сохранённую ссылку по id из кнопки (None — такой нет)."""
    row = (await session.execute(
        select(StashedLink).where(StashedLink.id == key))).scalar_one_or_none()
    if row is None:
        return None
    data = {"url": row.url, "chat_id": row.chat_id, "user_msg_id": row.user_msg_id,
            "premium": bool(row.premium), "kind": row.kind or "quality"}
    if row.payload:
        try:
            data.update(json.loads(row.payload))
        except ValueError:            # мусор в колонке не повод терять саму ссылку
            logger.warning("Не разобрал payload у кнопки %s", key)
    return data


async def save_ai_thread(session: AsyncSession, message_id: int, chat_id: int,
                         history: list[dict]) -> None:
    """Запоминает ветку разговора с ИИ. Старые (недельной давности) убираем: отвечать
    на прошлогоднее сообщение никто не станет, а таблица расти без предела не должна."""
    await session.merge(AiThread(message_id=message_id, chat_id=chat_id,
                                 history=json.dumps(history, ensure_ascii=False)))
    await session.execute(
        delete(AiThread).where(
            AiThread.at < datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)))
    await session.commit()


async def load_ai_thread(session: AsyncSession, message_id: int) -> list[dict] | None:
    """История разговора по id ответа бота (None — такой ветки нет)."""
    row = (await session.execute(
        select(AiThread).where(AiThread.message_id == message_id))).scalar_one_or_none()
    if row is None:
        return None
    try:
        return json.loads(row.history)
    except ValueError:
        logger.warning("Не разобрал историю ветки ИИ %s", message_id)
        return None


def _ai_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def ai_usage_today(session: AsyncSession, user_id: int) -> tuple[int, int]:
    """(запросов у этого человека, запросов всего) за сегодня."""
    day = _ai_day()
    mine = (await session.execute(
        select(func.coalesce(AiUsage.count, 0))
        .where(AiUsage.day == day, AiUsage.user_id == user_id))).scalar() or 0
    total = (await session.execute(
        select(func.coalesce(func.sum(AiUsage.count), 0))
        .where(AiUsage.day == day))).scalar() or 0
    return int(mine), int(total)


async def add_ai_usage(session: AsyncSession, user_id: int) -> None:
    """+1 к сегодняшнему счётчику. Вчерашние записи убираем — они больше не нужны."""
    day = _ai_day()
    # Прибавляем ОДНИМ запросом (UPDATE … count + 1), как у счётчика площадок: «прочитал,
    # прибавил, записал» терял параллельные попытки, а на первой попытке дня вставка
    # от двух сразу падала с IntegrityError – и попытка не засчитывалась вовсе.
    updated = (await session.execute(
        update(AiUsage).where(AiUsage.day == day, AiUsage.user_id == user_id)
        .values(count=func.coalesce(AiUsage.count, 0) + 1))).rowcount
    if not updated:
        session.add(AiUsage(day=day, user_id=user_id, count=1))
    await session.execute(delete(AiUsage).where(AiUsage.day != day))
    try:
        await session.commit()
    except IntegrityError:          # запись дня успел создать параллельный запрос
        await session.rollback()
        await session.execute(
            update(AiUsage).where(AiUsage.day == day, AiUsage.user_id == user_id)
            .values(count=func.coalesce(AiUsage.count, 0) + 1))
        await session.commit()


async def save_check_timings(session: AsyncSession, pairs: list[tuple[str, float]]) -> None:
    """Дописывает длительности проверок и подчищает хвост истории."""
    if not pairs:
        return
    session.add_all([CheckTiming(name=name, sec=float(sec)) for name, sec in pairs])
    await session.commit()

    # Чистим сразу здесь: отдельная фоновая уборка ради пары сотен строк не нужна.
    rows = (await session.execute(
        select(CheckTiming.id, CheckTiming.name).order_by(CheckTiming.id.desc())
    )).all()
    seen, stale = {}, []
    for row_id, name in rows:                     # идём от новых к старым
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > CHECK_HISTORY_KEEP:
            stale.append(row_id)
    if stale:
        await session.execute(delete(CheckTiming).where(CheckTiming.id.in_(stale)))
        await session.commit()


async def get_check_baselines(session: AsyncSession) -> dict[str, tuple[float, int]]:
    """Норма по каждой проверке: {название: (медиана секунд, сколько замеров)}.

    Медиана, а не среднее: скорость чужих серверов скачет, и одно случайное значение
    в десять раз больше обычного не должно задирать норму.
    """
    rows = (await session.execute(
        select(CheckTiming.name, CheckTiming.sec).order_by(CheckTiming.id.desc())
    )).all()
    buckets: dict[str, list[float]] = {}
    for name, sec in rows:
        bucket = buckets.setdefault(name, [])
        if len(bucket) < CHECK_HISTORY_KEEP:
            bucket.append(float(sec))
    return {name: (statistics.median(vals), len(vals)) for name, vals in buckets.items() if vals}
