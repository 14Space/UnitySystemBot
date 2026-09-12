"""
Расшифровка речи из голосовых сообщений и видео-кружков через faster-whisper.

Модель работает локально (на этом же сервере): ничего не уходит наружу, платить
не нужно. При первом запуске faster-whisper сам скачает выбранную модель с
HuggingFace (см. WHISPER_MODEL в config) и закэширует её на диске.

faster-whisper умеет читать аудио прямо из файла (.ogg голосового и .mp4 кружка)
через встроенный декодер, поэтому ручная конвертация ffmpeg не нужна.
"""
import ctypes
import gc
import logging
import os
import re
import threading

from bot.config import (
    WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE, WHISPER_LANGUAGES,
    WHISPER_LANG_MIN_PROB, WHISPER_MIN_SPEECH_PROB,
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
    vad_parameters=dict(min_silence_duration_ms=500),  # чуть агрессивнее режем тишину
)

# Титры-«галлюцинации»: Whisper на тишине/музыке дописывает заученные из обучающих
# данных подписи к видео («Субтитры сделал …», «Продолжение следует…», «Спасибо за
# просмотр», Amara.org и т.п.). Вырезаем их из результата, даже если модель выдала.
_HALLUCINATION_RE = re.compile(
    r"\s*(?:"
    r"субтитр\w*\s+(?:сделал|делал|створ\w*|підготув\w*|подготов\w*|редагув\w*|"
    r"редактир\w*|правил|предостав\w*|надав\w*|переклав|перевод\w*|от|by)[^\n]*"
    r"|(?:редактор|корректор)\s+субтитр\w*[^\n]*"
    r"|dimatorzok[^\n]*"
    r"|продолжение\s+следует[.!…\s]*"
    r"|продовження\s+(?:далі|буде)[.!…\s]*"
    r"|спасибо\s+за\s+просмотр[.!…\s]*"
    r"|дяку\w*\s+за\s+перегляд[.!…\s]*"
    r"|subtitles?\s+by[^\n]*"
    r"|amara\.?\s*org[^\n]*"
    r")\s*",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    """Убирает фразы-титры и лишние пробелы. Может вернуть пустую строку."""
    text = _HALLUCINATION_RE.sub(" ", text)
    return re.sub(r"\s{2,}", " ", text).strip()

# Модель тяжёлая — грузим её один раз и переиспользуем (ленивая инициализация).
# Блокировка защищает от гонки, если два сообщения придут одновременно.
_model = None
_model_lock = threading.Lock()
# GPU может «отвалиться» уже ПОСЛЕ загрузки модели (на Windows+WSL2 частая причина —
# ПК ушёл в сон/гибернацию, и контекст CUDA протух). Тогда каждая расшифровка падает
# с «CUDA failed…». Ловим это на лету и перегружаем модель на CPU: медленнее, но работает
# без ручного перезапуска. Флаг держит нас на CPU до перезапуска бота (там снова пробуем GPU).
_forced_cpu = False
# Битые GPU-модели складываем СЮДА и не отпускаем до конца жизни процесса. Освободить
# такую модель нельзя: её деструктор лезет в уже мёртвый контекст CUDA и роняет процесс
# аварийным завершением на уровне C++ («terminate called … CUDA failed»), а такое Python
# перехватить не может — падает весь бот, а не одна расшифровка. Память она занимает
# только на видеокарте, которой всё равно больше нет, так что цена нулевая.
_dead_models = []


def release_model():
    """Выгружает локальную модель и возвращает память системе.

    Нужна, когда прогрев выключен: модель поднимается под задачу, а между задачами
    незачем держать ~3.8 ГБ RAM и видеопамять. Проверка функционала её и поднимает,
    и отпускает.

    Про гонку с работающей расшифровкой: беспокоиться не о чем. Здесь мы снимаем лишь
    СВОЮ ссылку, а у выполняющегося сейчас вызова есть собственная — Python освободит
    объект только когда закончат все, кто им пользуется.

    Битые GPU-модели живут в _dead_models и сюда не попадают: освобождать модель
    с мёртвым контекстом CUDA нельзя, её деструктор роняет процесс целиком. Всё, что
    лежит в _model, исправно — в том числе модель на CPU после отката с видеокарты,
    и её тоже надо отпускать, иначе после отказа GPU память занята навсегда.
    """
    global _model
    with _model_lock:
        _model = None
    gc.collect()
    _trim_heap()


def _trim_heap():
    """Просит систему отдать освобождённую память ядру.

    Без этого «свободная» память остаётся за процессом: стандартный распределитель
    держит её про запас и операционной системе не возвращает. Для долгоживущего бота
    это выглядит как утечка — замер показал 3782 МБ после выгрузки модели на процессоре
    и 137 МБ после этого вызова.

    Работает только в glibc (обычный Linux). На других системах вызова просто нет,
    и тогда молча пропускаем.
    """
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def _is_cuda_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(k in s for k in ("cuda", "cublas", "cudnn", "gpu", "cufft", "nvrtc"))


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
    if _forced_cpu:                       # GPU уже подвёл на лету — грузим сразу на CPU
        device, compute_type = "cpu", "int8"
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


# «Безопасный» набор опций на случай, если «умный» режим упал. Известный сбой
# faster-whisper (IndexError: boolean index did not match…) возникает на некоторых
# записях из-за VAD-фильтра и пословных меток времени. Тут их выключаем: разбор
# грубее (без вырезания тишины), зато проходит. Опции, требующие word_timestamps
# (hallucination_silence_threshold), тоже убраны.
_SAFE_OPTS = dict(
    vad_filter=False,
    condition_on_previous_text=False,
    no_speech_threshold=0.6,
    log_prob_threshold=-1.0,
    compression_ratio_threshold=2.4,
)


def _run(model, file_path: str, language, opts) -> tuple[list, object]:
    """Запускает распознавание и СРАЗУ вычитывает сегменты (генератор ленивый —
    ошибки библиотеки вылезают именно при чтении). Возвращает список сегментов."""
    segments, info = model.transcribe(file_path, language=language, **opts)
    return list(segments), info


def transcribe(file_path: str) -> str:
    """
    Расшифровывает речь из файла (голосовое .ogg или кружок .mp4) в текст.

    Возвращает распознанный текст или пустую строку, если ничего не распознано
    (тишина, музыка без слов, неразборчивый звук). Вызывать в отдельном потоке
    через asyncio.to_thread — функция синхронная и долгая.

    Если GPU отвалился на лету (CUDA-сбой) — один раз перегружаем модель на CPU и
    повторяем, чтобы расшифровка продолжала работать без перезапуска бота.
    """
    global _model, _forced_cpu
    try:
        return _transcribe_once(file_path)
    except Exception as e:
        if _forced_cpu or not _is_cuda_error(e):
            raise                         # уже на CPU или сбой не про GPU — не наш случай
        logger.error("Whisper: сбой GPU (CUDA) на лету — перегружаю модель на CPU", exc_info=True)
        with _model_lock:
            _forced_cpu = True
            if _model is not None:
                _dead_models.append(_model)   # НЕ освобождаем: освобождение убьёт процесс
            _model = None                     # следующий _get_model поднимет модель на CPU
        return _transcribe_once(file_path)


def _transcribe_once(file_path: str) -> str:
    model = _get_model()

    try:
        # Сначала Whisper сам определяет язык (language=None).
        segments, info = _run(model, file_path, None, _DECODE_OPTS)

        if _ALLOWED_LANGS:
            detected = getattr(info, "language", None)
            prob = getattr(info, "language_probability", 1.0) or 0.0
            primary = _ALLOWED_LANGS[0]

            # --- Защита от фантомов на музыке/шуме (нет речи → молчим) ---
            # Whisper на звуке без речи определяет язык с низкой уверенностью и
            # ВЫДУМЫВАЕТ фразы. Признаки «речи нет»:
            #  • очень низкая уверенность определения языка (< порога), ЛИБО
            #  • определился явно посторонний язык (не разрешённый и не
            #    «кириллический брат») без высокой уверенности.
            # В этих случаях НЕ форсим разбор в ru (иначе получим выдумку) — молчим.
            keep = set(_ALLOWED_LANGS) | _CONFUSABLE
            if prob < WHISPER_MIN_SPEECH_PROB or (
                    detected not in keep and prob < WHISPER_LANG_MIN_PROB):
                logger.info("Речи не распознано (язык '%s', увер. %.2f) — молчу",
                            detected, prob)
                return ""

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
                segments, info = _run(model, file_path, primary, _DECODE_OPTS)
    except Exception:
        # «Умный» режим упал на этой записи (например, известный IndexError из-за
        # VAD/пословных меток) — не сдаёмся, повторяем в безопасном режиме.
        logger.warning("Whisper: сбой в основном режиме, повтор в безопасном", exc_info=True)
        segments, info = _run(model, file_path, None, _SAFE_OPTS)

    text = " ".join(segment.text.strip() for segment in segments).strip()
    return _clean(text)
