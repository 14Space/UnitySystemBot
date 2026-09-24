FROM python:3.12-slim

WORKDIR /app

# ffmpeg — конвертация видео/аудио. Шрифты Noto — чтобы карточки твитов (Chromium)
# правильно рисовали кириллицу и цветные эмодзи, а не «квадратики».
RUN apt-get update && apt-get install -y \
        ffmpeg fonts-noto-core fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Deno — JS-движок для решения новой защиты YouTube (без него часть роликов, включая
# Shorts, отдаёт «Sign in to confirm you're not a bot» даже с валидным PO-токеном).
# yt-dlp находит его сам по PATH, ничего в коде указывать не нужно.
#
# Берём готовый файл из официального образа, закреплённого по контрольной сумме.
# Раньше стояло «curl … | sh»: скрипт с сайта исполнялся не глядя, и подмени его
# кто-нибудь по дороге – он выполнился бы при сборке. Обновить deno = сменить
# версию и сумму ниже (docker buildx imagetools inspect denoland/deno:bin-<версия>).
COPY --from=denoland/deno:bin-2.9.7@sha256:bc5aa4466e21b6d3021226a85ba2e1911f7c386254d97b9d797903ab74edace2 \
     /deno /usr/local/bin/deno

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# yt-dlp: поверх стабильной из requirements ставим НОЧНУЮ сборку — только в ней есть
# скачивание через SABR (новый протокол YouTube). Стабильная это ещё не умеет и отдаёт
# 403 на популярных роликах. Плагин bgutil (клиент к «выдавателю пропусков», PO-токенам)
# уже стоит из requirements.txt — здесь не дублируем.
# Сбрасыватель кеша: Docker перекачивает этот файл при каждой сборке, и когда выходит
# новая ночная сборка, его содержимое меняется — тогда слой ниже пересобирается. Без
# этого строка pip не менялась неделями, слой брался из кеша, и ЛЮБАЯ пересборка ставила
# ту версию yt-dlp, что попала в образ первый раз (ловилось как «устарел на 3 недели»
# сразу после свежей сборки).
ADD https://api.github.com/repos/yt-dlp/yt-dlp-nightly-builds/releases/latest /tmp/ytdlp-nightly.json
# Ночные сборки yt-dlp публикуются на обычном PyPI как пре-релизы (--pre), поэтому
# сторонний индекс не нужен. Раньше тут стоял --extra-index-url: pip выбирает самую
# свежую версию из ВСЕХ индексов, и лишний индекс – лишний путь подсунуть пакет.
RUN pip install --no-cache-dir -U --pre "yt-dlp[default]"

# Убираем неиспользуемый SCRIPT-вариант POT-плагина bgutil: мы ходим к провайдеру по HTTP
# (сервис bgutil-provider), а script-вариант требует node и к тому же рассинхронён с ночным
# yt-dlp (ImportError: BgUtilPTPBase) — из-за него в лог сыпался шум при каждом запуске.
# HTTP-вариант это не задевает (он импортирует из нового API самого yt-dlp), YouTube работает.
RUN P=/usr/local/lib/python3.12/site-packages/yt_dlp_plugins/extractor \
    && rm -f "$P/getpot_bgutil_script.py" \
    && rm -rf "$P/__pycache__/getpot_bgutil_script"*

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
# Ручные инструменты (tools/speedbench.py): в работе бота не участвуют, но должны
# быть под рукой внутри контейнера — там стоят yt-dlp, ffmpeg и лежат куки.
COPY tools/ ./tools/

CMD ["python", "-m", "bot.main"]
