import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
# Локальный Telegram Bot API сервер (лимит файлов 2 ГБ вместо 50 МБ, быстрая отдача)
TELEGRAM_LOCAL_API_URL = os.getenv("TELEGRAM_LOCAL_API_URL", "http://localhost:8081")
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "data/downloads")

# Локальный Bot API сервер кладёт принятые файлы на диск и в getFile возвращает
# путь внутри СВОЕГО контейнера (TELEGRAM_BOT_API_ROOT). На хосте этот же том
# смонтирован в TELEGRAM_LOCAL_FILES_DIR — по нему мы и читаем файл напрямую,
# вместо скачивания по HTTP (которое для локального сервера не работает).
TELEGRAM_BOT_API_ROOT = os.getenv("TELEGRAM_BOT_API_ROOT", "/var/lib/telegram-bot-api")
TELEGRAM_LOCAL_FILES_DIR = os.getenv("TELEGRAM_LOCAL_FILES_DIR", "data/telegram-api")
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

# --- Расшифровка голосовых и кружков (faster-whisper, работает локально) ---
# Размер модели: tiny / base / small / medium / large-v3.
# Чем больше – тем точнее и тяжелее. На GPU (RTX 4060, 8 ГБ) тянем "large-v3".
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3")
# Где считать: "cuda" (видеокарта NVIDIA, быстро) или "cpu".
# Если CUDA не заведётся – код сам откатится на CPU.
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cuda")
# Тип вычислений: для GPU – "float16", для CPU – "int8".
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "float16")
# Разрешённые языки распознавания (через запятую). Whisper определяет язык сам,
# но если он не из этого списка – берём первый из списка. Пусто = любой язык.
WHISPER_LANGUAGES = os.getenv("WHISPER_LANGUAGES", "ru,uk")
# Прогревать модель при старте бота (грузит её в видеопамять сразу, ~3 ГБ).
# true – первое голосовое не тормозит, но видеопамять занята всё время работы.
# false – видеопамять свободна, пока никто не прислал голосовое (как было раньше).
WHISPER_PREWARM = os.getenv("WHISPER_PREWARM", "true").lower() in ("1", "true", "yes")
