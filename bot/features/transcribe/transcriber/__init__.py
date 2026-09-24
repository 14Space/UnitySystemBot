from bot.features.transcribe.transcriber.whisper_transcriber import warmup
from bot.features.transcribe.transcriber.cascade import transcribe_audio

# Точки входа пакета: прогрев модели берёт main.py, расшифровку – обработчик голосовых.
__all__ = ["warmup", "transcribe_audio"]
