from sqlalchemy import Column, Integer, String, Boolean, DateTime, func
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class User(Base):
    """Пользователь бота"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, unique=True, nullable=False)
    username = Column(String, nullable=True)
    is_premium = Column(Boolean, default=False)
    language = Column(String, default="ru")
    created_at = Column(DateTime, server_default=func.now())


class DownloadStat(Base):
    """Счётчик запросов по платформам (для статистики популярности)"""
    __tablename__ = "download_stats"

    platform = Column(String, primary_key=True)
    count = Column(Integer, default=0)


class CachedFile(Base):
    """Кэш: URL+качество → file_id Telegram (файл лежит на серверах Telegram)"""
    __tablename__ = "cached_files"

    id = Column(Integer, primary_key=True)
    url_hash = Column(String, unique=True, nullable=False)
    original_url = Column(String, nullable=False)
    file_id = Column(String, nullable=False)
    quality = Column(String, nullable=True)
    cached_at = Column(DateTime, server_default=func.now())
