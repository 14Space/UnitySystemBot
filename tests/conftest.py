"""Общая подготовка для тестов.

Код бота читает настройки из окружения прямо при импорте (bot.config). На машине,
где тесты гоняются без .env (например, в GitHub Actions), импорт бы упал ещё до
первой проверки — поэтому подставляем безобидные значения заранее.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BOT_TOKEN", "0:test")
os.environ.setdefault("ADMIN_ID", "0")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_chat_settings_cache():
    """Настройки чата бот держит в памяти минуту (repository._CHAT_CACHE), а тесты
    заводят каждый свою временную базу – без сброса чат 5 из одного теста подсмотрел
    бы настройки чата 5 из другого."""
    from bot.database.repository import forget_chat_settings
    forget_chat_settings()
    yield
    forget_chat_settings()


@pytest.fixture(autouse=True)
def _home_tunnel_alive(monkeypatch):  # ВРЕМЕННО home_tunnel
    """Тесты в сеть не ходят: домашний туннель для них всегда «жив»."""
    from bot.utils import home_tunnel
    monkeypatch.setattr(home_tunnel, "_down", None)
    monkeypatch.setattr(home_tunnel, "_probe_sync", lambda proxy: True)
