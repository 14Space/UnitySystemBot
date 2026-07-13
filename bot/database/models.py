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


class MonthlyTraffic(Base):
    """Сколько байт скачано (записано на диск) за месяц — для оценки износа SSD.
    Кэш-переотправки сюда не попадают: они на диск ничего не пишут."""
    __tablename__ = "monthly_traffic"

    month = Column(String, primary_key=True)  # "YYYY-MM" (UTC)
    total_bytes = Column(Integer, default=0)
    files = Column(Integer, default=0)


class ChatSettings(Base):
    """Настройки функций по чату (для /setconfig в @viaUnitySystem).
    disabled_features — какие функции выключены в этом чате, через запятую."""
    __tablename__ = "chat_settings"

    chat_id = Column(Integer, primary_key=True)
    disabled_features = Column(String, default="")
    # Режим слайдшоу TikTok в этой группе: video | photos | ask (по умолчанию video)
    slideshow_mode = Column(String, default="video")
    # Валюты, в которые конвертер переводит в этой группе (коды через запятую)
    currency_targets = Column(String, default="USD,EUR")
    # Слать ли отдельным аудио дорожку к видео «лёгких» платформ (по умолчанию нет)
    audio_track = Column(Boolean, default=False)


class CachedFile(Base):
    """Кэш: URL+качество → file_id Telegram (файл лежит на серверах Telegram)"""
    __tablename__ = "cached_files"

    id = Column(Integer, primary_key=True)
    url_hash = Column(String, unique=True, nullable=False)
    original_url = Column(String, nullable=False)
    file_id = Column(String, nullable=False)
    quality = Column(String, nullable=True)
    # file_id в Telegram привязан к боту, который отправил файл: чужой id невалиден.
    # Поэтому кэш храним по каждому боту отдельно.
    bot_id = Column(Integer, nullable=True)
    cached_at = Column(DateTime, server_default=func.now())
