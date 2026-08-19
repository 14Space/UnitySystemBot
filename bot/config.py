import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
# Дополнительные боты (общий мотor, ресурсы не дублируются). Пусто = бот не запускается.
# Пока пусто — @viaSaver умеет всё (скачивание + расшифровка). Как только появятся эти
# токены — @viaSaver станет «только скачивание», расшифровка уедет в @viaTranscription,
# а @viaUnitySystem получит все функции + /setconfig по группам.
TRANSCRIBE_BOT_TOKEN = os.getenv("TRANSCRIBE_BOT_TOKEN", "")
UNITY_BOT_TOKEN = os.getenv("UNITY_BOT_TOKEN", "")
# Пусто — конвертер валют живёт внутри @viaUnitySystem. Появится токен — станет
# отдельным ботом @viaCurrency (по образцу viaVoice), без рефакторинга.
CURRENCY_BOT_TOKEN = os.getenv("CURRENCY_BOT_TOKEN", "")

# --- ИИ-ассистент (/ai). Бесплатные провайдеры: Gemini (Google AI Studio) и Groq. ---
# Ключи бесплатные: Gemini — aistudio.google.com/apikey, Groq — console.groq.com/keys.
# Без ключей команда /ai отвечает «ИИ не настроен». Пробуем провайдеров по очереди.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
# Модели держим актуальными: провайдеры снимают старые с хостинга без обратной
# совместимости (gemini-2.0-flash и llama-3.3-70b-versatile отключены — /ai падал в 404).
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
# Свои суточные лимиты — чтобы держаться в бесплатном тире (сброс в полночь UTC).
AI_DAILY_LIMIT = int(os.getenv("AI_DAILY_LIMIT", "1000"))       # всего запросов в сутки
AI_USER_DAILY_LIMIT = int(os.getenv("AI_USER_DAILY_LIMIT", "30"))  # на пользователя в сутки

# Локальный Telegram Bot API сервер (лимит файлов 2 ГБ вместо 50 МБ, быстрая отдача)
TELEGRAM_LOCAL_API_URL = os.getenv("TELEGRAM_LOCAL_API_URL", "http://localhost:8081")
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "data/downloads")

# Локальный Bot API сервер кладёт принятые файлы на диск (внутри своего Docker-тома)
# и в getFile возвращает путь внутри контейнера (TELEGRAM_BOT_API_ROOT). По HTTP
# локальный сервер файлы не отдаёт, поэтому бот забирает их из контейнера:
#  • если бот сам в Docker и том примонтирован — читает напрямую с диска;
#  • если бот на хосте (Windows) — копирует из контейнера через `docker cp`.
TELEGRAM_BOT_API_ROOT = os.getenv("TELEGRAM_BOT_API_ROOT", "/var/lib/telegram-bot-api")
TELEGRAM_LOCAL_FILES_DIR = os.getenv("TELEGRAM_LOCAL_FILES_DIR", "data/telegram-api")
# Имя контейнера локального Bot API (для `docker cp`, когда бот запущен на хосте).
TELEGRAM_API_CONTAINER = os.getenv("TELEGRAM_API_CONTAINER", "unitysystem-telegram-bot-api-1")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/unitysystem.db")

# Spotify API — только для чтения метаданных трека (название, исполнитель)
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

# Telegram ID администратора — кому доступна команда /stats и дневной отчёт.
# Узнать свой ID можно у бота @userinfobot. 0 = выключено.
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Дневной отчёт (статистика + проверка функционала) шлём строго в это время по
# часовому поясу админа. Молдова дважды в год переводит часы — берём IANA-зону
# Europe/Chisinau, и zoneinfo сам учитывает переход на летнее/зимнее время
# (пакет tzdata в requirements гарантирует базу зон на любой ОС/архитектуре).
ADMIN_TZ = os.getenv("ADMIN_TZ", "Europe/Chisinau")
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "12"))     # час (0..23) локального времени админа

# Сколько загрузок может идти одновременно на всех (тюнить под мощность сервера)
MAX_PARALLEL_DOWNLOADS = int(os.getenv("MAX_PARALLEL_DOWNLOADS", "5"))

# Цена Premium в Telegram Stars (разовый платёж). ~250⭐ ≈ 5$
PREMIUM_PRICE_STARS = int(os.getenv("PREMIUM_PRICE_STARS", "250"))

# Слайдшоу TikTok: сколько секунд показывается один слайд. Фиксированно, как в самом
# TikTok (~3с). Делить длину музыки на число картинок нельзя: 3 слайда + трек на 3 минуты
# давали по минуте на слайд. Музыка обрезается под длину видео и гасится в конце.
SLIDE_SEC = float(os.getenv("SLIDE_SEC", "3.0"))
# Длительность затухания музыки в конце слайдшоу (секунды), 0 = без затухания
SLIDE_AUDIO_FADE_SEC = float(os.getenv("SLIDE_AUDIO_FADE_SEC", "1.5"))

# Куки залогиненного Instagram-аккаунта (формат Netscape, cookies.txt). Нужны, чтобы
# скачивать Reels/посты с пометкой «доступ не для всех» (видны только вошедшим в аккаунт).
# Экспортировать можно расширением «Get cookies.txt» из браузера, где выполнен вход в IG.
# Пусто/файла нет = ходим анонимно (только публичный контент).
INSTAGRAM_COOKIES = os.getenv("INSTAGRAM_COOKIES", "data/instagram_cookies.txt")

# Куки залогиненного аккаунта X (Twitter), формат Netscape. Нужны как запасной путь
# для видео и контента, видимого только вошедшим. Публичные посты берём и без них.
X_COOKIES = os.getenv("X_COOKIES", "data/x.com_cookies.txt")

# Прокси для обхода гео-блокировок YouTube/YT Music (http:// или socks5://).
# Пусто = без прокси. Пример: socks5://127.0.0.1:1080
PROXY_URL = os.getenv("PROXY_URL", "")

# Прокси ТОЛЬКО для Instagram (http:// или socks5://). Instagram банит по IP: с
# домашнего (резидентного) IP всё качается напрямую, а с дата-центрового (VPS)
# прилетает 403/429. Поэтому логика такая: сначала пробуем НАПРЯМУЮ, и лишь если
# Instagram отказал по анти-боту — повторяем через этот прокси. Пусто = только напрямую
# (правильно для домашнего ПК; на VPS сюда вписать рабочий прокси). Можно указать тот же,
# что и PROXY_URL — например: INSTAGRAM_PROXY=${PROXY_URL} в .env.
INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "")

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
# ВАЖНО: первый язык в списке – основной (запасной при сомнениях). Сейчас это ru.
WHISPER_LANGUAGES = os.getenv("WHISPER_LANGUAGES", "ru,uk")
# Порог уверенности автоопределения языка (0..1). Русский и украинский очень
# похожи, и Whisper их путает. Если уверенность ниже порога – берём основной
# язык (первый в WHISPER_LANGUAGES, т.е. ru). Выше порог = чаще русский.
WHISPER_LANG_MIN_PROB = float(os.getenv("WHISPER_LANG_MIN_PROB", "0.85"))
# Порог «есть ли вообще речь» (0..1). Whisper на музыке/шуме определяет язык с
# очень низкой уверенностью и ВЫДУМЫВАЕТ фразы («Девушки отдыхают…»). Если
# уверенность определения языка ниже этого порога – считаем, что речи нет, и
# молчим. Настоящая речь обычно даёт 0.9+; музыка/шум – 0.1..0.6. Выше порог =
# строже отсекаем фантомы (но можно случайно срезать очень тихую речь).
WHISPER_MIN_SPEECH_PROB = float(os.getenv("WHISPER_MIN_SPEECH_PROB", "0.5"))
# Прогревать модель при старте бота (грузит её в видеопамять сразу, ~3 ГБ).
# true – первое голосовое не тормозит, но видеопамять занята всё время работы.
# false – видеопамять свободна, пока никто не прислал голосовое (как было раньше).
WHISPER_PREWARM = os.getenv("WHISPER_PREWARM", "true").lower() in ("1", "true", "yes")
