FROM python:3.12-slim

WORKDIR /app

# Устанавливаем ffmpeg — нужен для конвертации видео/аудио
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/
COPY worker/ ./worker/

CMD ["python", "-m", "bot.main"]
