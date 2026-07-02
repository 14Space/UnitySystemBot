import hashlib
from datetime import datetime, timezone
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from bot.database.models import User, CachedFile, DownloadStat, MonthlyTraffic


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


async def get_cached_file_id(session: AsyncSession, url: str, quality: str = None) -> str | None:
    """Возвращает Telegram file_id для пары URL+качество, если он уже в кэше"""
    url_hash = hashlib.md5(f"{url}:{quality}".encode()).hexdigest()
    result = await session.execute(select(CachedFile).where(CachedFile.url_hash == url_hash))
    cached = result.scalar_one_or_none()
    return cached.file_id if cached else None


async def get_any_cached_file(session: AsyncSession, url: str) -> dict | None:
    """Любой готовый файл для этой ссылки (для inline-режима). Возвращает file_id и тип."""
    result = await session.execute(
        select(CachedFile).where(CachedFile.original_url == url).limit(1)
    )
    row = result.scalars().first()
    if not row:
        return None
    return {"file_id": row.file_id, "quality": row.quality or ""}


async def clear_cache(session: AsyncSession) -> int:
    """Удаляет весь кэш file_id. Возвращает число удалённых записей.
    Сами файлы не трогаются — они на серверах Telegram; стираются лишь ссылки на них."""
    from sqlalchemy import delete
    count = (await session.execute(select(func.count(CachedFile.id)))).scalar() or 0
    await session.execute(delete(CachedFile))
    await session.commit()
    return count


async def save_cached_file_id(session: AsyncSession, url: str, file_id: str, quality: str = None) -> None:
    """Сохраняет file_id в кэш (или обновляет, если запись уже есть)"""
    url_hash = hashlib.md5(f"{url}:{quality}".encode()).hexdigest()
    existing = await session.execute(select(CachedFile).where(CachedFile.url_hash == url_hash))
    row = existing.scalar_one_or_none()
    if row:
        row.file_id = file_id
    else:
        session.add(CachedFile(url_hash=url_hash, original_url=url, file_id=file_id, quality=quality))
    await session.commit()
