import hashlib
from contextvars import ContextVar
from datetime import datetime, timezone
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from bot.database.models import User, CachedFile, DownloadStat, MonthlyTraffic, ChatSettings

# id бота, обрабатывающего текущий апдейт. Выставляет RoutingMiddleware на каждый
# апдейт. file_id в Telegram привязан к отправившему боту, поэтому кэш ведём отдельно
# по каждому боту — и в ключе (url_hash), и колонкой bot_id.
current_bot_id: ContextVar[int | None] = ContextVar("current_bot_id", default=None)


def _cache_hash(url: str, quality, bot_id: int | None) -> str:
    return hashlib.md5(f"{bot_id}:{url}:{quality}".encode()).hexdigest()


async def get_or_create_user(
    session: AsyncSession, user_id: int, username: str, language: str = "ru"
) -> User:
    """Возвращает пользователя из БД, или создаёт нового"""
    result = await session.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(user_id=user_id, username=username, language=language)
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            # Гонка: пользователя уже создал параллельный запрос — берём его
            await session.rollback()
            result = await session.execute(select(User).where(User.user_id == user_id))
            user = result.scalar_one()
    return user


async def is_premium(session: AsyncSession, user_id: int) -> bool:
    """Есть ли у пользователя Premium"""
    result = await session.execute(select(User.is_premium).where(User.user_id == user_id))
    return bool(result.scalar_one_or_none())


async def set_premium(session: AsyncSession, user_id: int, value: bool = True) -> None:
    """Включает/выключает Premium у пользователя"""
    result = await session.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if user:
        user.is_premium = value
        await session.commit()


async def increment_download(session: AsyncSession, platform: str) -> None:
    """Увеличивает счётчик запросов для платформы"""
    row = await session.execute(
        select(DownloadStat).where(DownloadStat.platform == platform)
    )
    stat = row.scalar_one_or_none()
    if stat:
        stat.count += 1
    else:
        session.add(DownloadStat(platform=platform, count=1))
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

    lang_rows = (await session.execute(
        select(User.language, func.count(User.id)).group_by(User.language)
    )).all()
    languages = {(lang or "?"): cnt for lang, cnt in lang_rows}

    return {
        "downloads": downloads,
        "total_downloads": sum(downloads.values()),
        "users": users,
        "languages": languages,
        "traffic": await get_traffic(session),
    }


async def get_disabled_features(session: AsyncSession, chat_id: int) -> set[str]:
    """Множество выключенных в этом чате функций (пусто = всё включено)."""
    row = (await session.execute(
        select(ChatSettings).where(ChatSettings.chat_id == chat_id)
    )).scalar_one_or_none()
    if not row or not row.disabled_features:
        return set()
    return {f for f in row.disabled_features.split(",") if f}


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


async def get_slideshow_mode(session: AsyncSession, chat_id: int) -> str:
    """Режим слайдшоу TikTok в этой группе: video | photos | ask (по умолчанию video)."""
    row = (await session.execute(
        select(ChatSettings).where(ChatSettings.chat_id == chat_id)
    )).scalar_one_or_none()
    return row.slideshow_mode if row and row.slideshow_mode else "video"


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
    """Включена ли отправка аудиодорожки к видео в этой группе (по умолчанию нет)."""
    row = (await session.execute(
        select(ChatSettings).where(ChatSettings.chat_id == chat_id)
    )).scalar_one_or_none()
    return bool(row and row.audio_track)


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
        .where(CachedFile.original_url == url, CachedFile.bot_id == bot_id)
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
        session.add(CachedFile(url_hash=url_hash, original_url=url,
                               file_id=file_id, quality=quality, bot_id=bot_id))
    await session.commit()
