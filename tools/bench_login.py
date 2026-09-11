"""
Разовый вход в твой Telegram-аккаунт для замеров (Telethon).

Запускаешь ТЫ САМ: скрипт спросит номер телефона и код из Telegram.
Код вводишь только ты — он никуда не сохраняется и не передаётся.

После успешного входа рядом появится файл сессии (bench.session) — он даёт доступ
к аккаунту, поэтому лежит во временной папке, а не в проекте. Когда замеры закончим,
его можно просто удалить.
"""
import os
import re
import sys

from telethon.sync import TelegramClient

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = os.path.join(HERE, "bench")
ENV = r"C:\PROJECTS\UnitySystemBot\.env"


def env_value(name: str) -> str:
    """Берём API_ID/API_HASH из .env проекта (это ключи приложения, не пароль)."""
    try:
        with open(ENV, encoding="utf-8") as f:
            for line in f:
                m = re.match(rf"\s*{name}\s*=\s*(.+?)\s*$", line)
                if m:
                    return m.group(1).strip()
    except OSError:
        pass
    return ""


api_id, api_hash = env_value("TELEGRAM_API_ID"), env_value("TELEGRAM_API_HASH")
if not api_id or not api_hash:
    sys.exit("Не нашёл TELEGRAM_API_ID / TELEGRAM_API_HASH в .env")

print("Вход в Telegram для замеров. Сейчас спросит номер телефона и код из Telegram.")
print("Код вводишь только ты — он нигде не сохраняется.\n")

with TelegramClient(SESSION, int(api_id), api_hash) as client:
    me = client.get_me()
    print(f"\nГотово! Вошёл как: {me.first_name} (@{me.username or 'без username'})")
    print(f"Файл сессии: {SESSION}.session")
    print("Теперь можно запускать замеры.")
