"""
Разовый вход в Telegram-аккаунт для ПОСРЕДНИКА (Telethon).

Посредник — это аккаунт, от чьего имени бот пишет чужому боту-расшифровщику. Нужен
потому, что бот НЕ МОЖЕТ написать боту, это запрет самого Telegram.

Запускаешь ТЫ САМ: скрипт спросит номер телефона и код из Telegram (а если включена
двухэтапная проверка — ещё и облачный пароль). Ничего из этого никуда не сохраняется
и никому не передаётся, кроме самого Telegram.

Когда нужен: сессия перестала работать. Файл сессии переживает её отзыв, поэтому на
диске всё выглядит целым, а входа уже нет — например, после «Завершить все сеансы»
в настройках Telegram. Проверка функционала ловит это пунктом «Расшифровка (посредник)».

Запуск из корня проекта:
    python tools/relay_login.py

Файл сессии (data/relay.session) — это ПОЛНЫЙ доступ к аккаунту, как пароль. Он
закрыт в .gitignore и .dockerignore; никому его не пересылай.

После входа на сервере: файл лежит в data/, а она монтируется в контейнер томом —
достаточно скопировать его на сервер и перезапустить бота, пересборка не нужна.
"""
import os
import re
import sys

from telethon.sync import TelegramClient

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENV = os.path.join(ROOT, ".env")
# Telethon сам добавит .session — путь даём без расширения.
SESSION = os.path.join(ROOT, "data", "relay")


def env_value(name: str) -> str:
    """Берём API_ID/API_HASH из .env проекта (это ключи приложения, не пароль)."""
    try:
        with open(ENV, encoding="utf-8") as f:
            for line in f:
                m = re.match(rf"\s*{name}\s*=\s*(.+?)\s*$", line)
                if m:
                    # Отрезаем комментарий в конце строки, если он есть.
                    return m.group(1).split("#")[0].strip()
    except OSError:
        pass
    return ""


api_id, api_hash = env_value("TELEGRAM_API_ID"), env_value("TELEGRAM_API_HASH")
if not api_id or not api_hash:
    sys.exit("Не нашёл TELEGRAM_API_ID / TELEGRAM_API_HASH в .env")

os.makedirs(os.path.dirname(SESSION), exist_ok=True)

print("Вход в Telegram для аккаунта-посредника (расшифровка чужим ботом).")
print("Сейчас спросит номер телефона и код из Telegram.")
print("Код вводишь только ты — он нигде не сохраняется.\n")

with TelegramClient(SESSION, int(api_id), api_hash) as client:
    me = client.get_me()
    print(f"\nГотово! Вошёл как: {me.first_name} (@{me.username or 'без username'})")
    print(f"Файл сессии: {SESSION}.session")
    print("Проверить можно командой /test — пункт «Расшифровка (посредник)».")
