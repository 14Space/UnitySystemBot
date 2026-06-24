import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
# Локальный Telegram Bot API сервер (лимит файлов 2 ГБ вместо 50 МБ, быстрая отдача)
TELEGRAM_LOCAL_API_URL = os.getenv("TELEGRAM_LOCAL_API_URL", "http://localhost:8081")
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "data/downloads")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///bot.db")

# Spotify API — только для чтения метаданных трека (название, исполнитель)
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

# Telegram ID администратора — кому доступна команда /stats и дневной отчёт.
# Узнать свой ID можно у бота @userinfobot. 0 = выключено.
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Сколько загрузок может идти одновременно на всех (тюнить под мощность сервера)
MAX_PARALLEL_DOWNLOADS = int(os.getenv("MAX_PARALLEL_DOWNLOADS", "5"))

# Цена Premium в Telegram Stars (разовый платёж). ~250⭐ ≈ 5$
PREMIUM_PRICE_STARS = int(os.getenv("PREMIUM_PRICE_STARS", "250"))

# Прокси для обхода гео-блокировок YouTube/YT Music (http:// или socks5://).
# Пусто = без прокси. Пример: socks5://127.0.0.1:1080
PROXY_URL = os.getenv("PROXY_URL", "")
