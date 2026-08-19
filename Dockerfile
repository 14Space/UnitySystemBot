FROM python:3.12-slim

WORKDIR /app

# ffmpeg — конвертация видео/аудио. Шрифты Noto — чтобы карточки твитов (Chromium)
# правильно рисовали кириллицу и цветные эмодзи, а не «квадратики».
RUN apt-get update && apt-get install -y \
        ffmpeg fonts-noto-core fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# yt-dlp: ставим НОЧНУЮ сборку — только в ней есть скачивание через SABR (новый
# протокол YouTube). Стабильная это ещё не умеет и отдаёт 403 на популярных роликах.
# Плюс плагин bgutil — клиент к «выдавателю пропусков» (PO-токенов), без пропусков
# YouTube тоже отдаёт 403. Провайдер — отдельный сервис bgutil-provider в compose.
RUN pip install --no-cache-dir -U --pre "yt-dlp[default]" \
        --extra-index-url https://github.com/yt-dlp/yt-dlp-nightly-builds/releases \
    && pip install --no-cache-dir bgutil-ytdlp-pot-provider

# Браузер Chromium для Playwright (рендер карточек твитов) + его системные зависимости.
RUN playwright install --with-deps chromium

# Whisper на видеокарте (cuda): в Linux-контейнере CUDA-библиотеки из pip-пакетов
# (nvidia-cublas-cu12, nvidia-cudnn-cu12) лежат в site-packages и не видны загрузчику
# по умолчанию. На Windows это решал _add_cuda_dll_dirs(); в Linux указываем путь
# через LD_LIBRARY_PATH, иначе ctranslate2 не найдёт cublas/cudnn и Whisper уйдёт на CPU.
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib:/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib

# Кэш модели Whisper (~3 ГБ) — кладём в /cache, том монтируется в docker-compose,
# чтобы модель скачивалась один раз и переживала пересборку образа.
ENV HF_HOME=/cache

COPY bot/ ./bot/

CMD ["python", "-m", "bot.main"]
