"""
Расшифровка речи из голосовых сообщений и видео-кружков через faster-whisper.

Модель работает локально (на этом же сервере): ничего не уходит наружу, платить
не нужно. При первом запуске faster-whisper сам скачает выбранную модель с
HuggingFace (см. WHISPER_MODEL в config) и закэширует её на диске.

faster-whisper умеет читать аудио прямо из файла (.ogg голосового и .mp4 кружка)
через встроенный декодер, поэтому ручная конвертация ffmpeg не нужна.
"""
import logging
import os
import threading

from bot.config import (
    WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE, WHISPER_LANGUAGES,
    WHISPER_LANG_MIN_PROB,
)

logger = logging.getLogger(__name__)

# Разрешённые языки: ["ru", "uk"]. Если Whisper определит другой – берём первый.
_ALLOWED_LANGS = [x.strip() for x in WHISPER_LANGUAGES.split(",") if x.strip()]

# Кириллические «братья», которых Whisper легко путает между собой. Порог уверенности
# (WHISPER_LANG_MIN_PROB) применяем ТОЛЬКО внутри этой группы: если при низкой уверенности
# определился, скажем, uk вместо ru — берём основной (ru). А вот явно другой язык (en)
# при низкой уверенности НЕ ломаем переводом в русский — принимаем как есть.
_CONFUSABLE = {"ru", "uk", "be", "bg"}

# --- Защита от «галлюцинаций» -------------------------------------------------
# Whisper на шуме/музыке/невнятной речи не молчит, а ВЫДУМЫВАЕТ частые фразы
# («Продолжение следует…», «Спасибо за просмотр»). Эти параметры говорят модели
# «лучше отдай пусто, чем выдумывай». Минус: очень тихую/невнятную НАСТОЯЩУЮ речь
# тоже может пропустить. Всё тут можно крутить или убрать — поведение изменится.
_DECODE_OPTS = dict(
    vad_filter=True,                  # отсекаем участки без голоса (тишина/шум)
    condition_on_previous_text=False, # не «додумывать» по уже распознанному — частая
                                      # причина зацикленных выдуманных фраз в конце
    no_speech_threshold=0.6,          # выше порог = чаще решаем «речи нет» → пусто
    log_prob_threshold=-1.0,          # отбрасываем сегменты, в которых модель не уверена
    compression_ratio_threshold=2.4,  # режем повторяющийся «зацикленный» бред
    word_timestamps=True,             # нужно для проверки тишины ниже
    hallucination_silence_threshold=2.0,  # пропускаем подозрительные «фразы» в тишине
)

# Модель тяжёлая — грузим её один раз и переиспользуем (ленивая инициализация).
# Блокировка защищает от гонки, если два сообщения придут одновременно.
_model = None
_model_lock = threading.Lock()


def _add_cuda_dll_dirs():
    """
    На Windows DLL CUDA из pip-пакетов (nvidia-cublas-cu12, nvidia-cudnn-cu12)
    лежат внутри site-packages и не видны загрузчику. Добавляем их в путь поиска,
    иначе ctranslate2 не найдёт cublas/cudnn и упадёт при device='cuda'.
    """
    if os.name != "nt":
        return
    try:
        import nvidia  # пакеты CUDA ставят неймспейс-пакет nvidia
    except ImportError:
        return
    for base in nvidia.__path__:
        for sub in ("cublas", "cudnn", "cuda_nvrtc"):
            bin_dir = os.path.join(base, sub, "bin")
            if os.path.isdir(bin_dir):
                try:
                    os.add_dll_directory(bin_dir)
                except Exception:
                    pass
                # ctranslate2 грузит cublas через обычный поиск по PATH, поэтому
                # add_dll_directory мало — добавляем папку ещё и в PATH.
                if bin_dir not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


def _load_model():
    from faster_whisper import WhisperModel

    device = WHISPER_DEVICE
    compute_type = WHISPER_COMPUTE_TYPE
    if device == "cuda":
        _add_cuda_dll_dirs()
        try:
            logger.info("Загружаю модель Whisper '%s' на GPU (cuda)...", WHISPER_MODEL)
            return WhisperModel(WHISPER_MODEL, device="cuda", compute_type=compute_type)
        except Exception:
            # GPU не завёлся (нет CUDA-библиотек, мало памяти) — откатываемся на CPU
            logger.exception("CUDA недоступна, перехожу на CPU")
            device, compute_type = "cpu", "int8"

    logger.info("Загружаю модель Whisper '%s' на CPU...", WHISPER_MODEL)
    return WhisperModel(WHISPER_MODEL, device=device, compute_type=compute_type)


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _model = _load_model()
                logger.info("Модель Whisper загружена.")
    return _model


def warmup():
    """Заранее загружает модель в память видеокарты, чтобы первое голосовое после
    запуска бота не тормозило. Вызывать в отдельном потоке (asyncio.to_thread)."""
    try:
        _get_model()
    except Exception:
        logger.exception("Не удалось прогреть модель Whisper")


def transcribe(file_path: str) -> str:
    """
    Расшифровывает речь из файла (голосовое .ogg или кружок .mp4) в текст.

    Возвращает распознанный текст или пустую строку, если ничего не распознано
    (тишина, музыка без слов, неразборчивый звук). Вызывать в отдельном потоке
    через asyncio.to_thread — функция синхронная и долгая.
    """
    model = _get_model()

    # Сначала Whisper сам определяет язык (language=None).
    segments, info = model.transcribe(file_path, language=None, **_DECODE_OPTS)

    if _ALLOWED_LANGS:
        detected = getattr(info, "language", None)
        prob = getattr(info, "language_probability", 1.0) or 0.0
        primary = _ALLOWED_LANGS[0]
        # Когда откатываемся в основной язык (ru):
        #  • язык не из разрешённых — берём основной;
        #  • ИЛИ это «кириллический брат» (ru/uk) с низкой уверенностью — лечим путаницу.
        # Явно другой язык (en) при низкой уверенности НЕ трогаем: насильный перевод
        # английского в русский даёт мусор ("Продолжение следует...").
        confusable_doubt = (
            detected in _CONFUSABLE and primary in _CONFUSABLE
            and prob < WHISPER_LANG_MIN_PROB
        )
        if detected not in _ALLOWED_LANGS or confusable_doubt:
            logger.info(
                "Язык '%s' (увер. %.2f) ненадёжен — расшифровываю как '%s'",
                detected, prob, primary,
            )
            segments, info = model.transcribe(file_path, language=primary, **_DECODE_OPTS)

    text = " ".join(segment.text.strip() for segment in segments).strip()
    return text
