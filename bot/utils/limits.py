import asyncio

from bot.utils.i18n import t

# --- «Умные» лимиты одновременных задач ------------------------------------
#
# Задачи делятся на три группы, у каждой свой потолок:
#   • heavy      – тяжёлые видео (YouTube, PornHub, HDRezka): грузят CPU и канал
#   • light      – лёгкое (reels, shorts, фото, музыка, TikTok): быстрое
#   • transcribe – расшифровка ГС/кружков: грузит видеокарту
#
# «Умность» в том, что лёгкие задачи и расшифровка получают БОЛЬШЕ слотов, когда
# тяжёлых видео сейчас не качается (ноут свободнее). Примеры из задумки:
#   тяжёлых > 0:  heavy≤2,  light≤4,  transcribe≤1
#   тяжёлых = 0:           light≤6,  transcribe≤2
HEAVY = "heavy"
LIGHT = "light"
TRANSCRIBE = "transcribe"

HEAVY_MAX = 2          # тяжёлых видео одновременно – всегда не больше этого
LIGHT_BASE = 4         # лёгких, когда идёт тяжёлое видео
LIGHT_BOOST = 6        # лёгких, когда тяжёлых нет (берут их слоты)
TRANSCRIBE_BASE = 1    # расшифровок, когда идёт тяжёлое видео
TRANSCRIBE_BOOST = 2   # расшифровок, когда тяжёлых нет

# Предел размера файла (лимит локального Telegram Bot API — 2 ГБ, берём с запасом).
MAX_FILE_BYTES = 1_950_000_000


class FileTooLargeError(Exception):
    """Файл больше лимита Telegram (2 ГБ)."""


class _SmartLimiter:
    """Считает активные задачи по группам и пускает новые только в пределах
    динамического потолка. Ждущие просыпаются, как только слот освобождается."""

    def __init__(self):
        self._cond = asyncio.Condition()
        self._active = {HEAVY: 0, LIGHT: 0, TRANSCRIBE: 0}

    def _cap(self, category: str) -> int:
        heavy_idle = self._active[HEAVY] == 0
        if category == HEAVY:
            return HEAVY_MAX
        if category == LIGHT:
            return LIGHT_BOOST if heavy_idle else LIGHT_BASE
        if category == TRANSCRIBE:
            return TRANSCRIBE_BOOST if heavy_idle else TRANSCRIBE_BASE
        return 0

    async def acquire(self, category: str):
        async with self._cond:
            # Ждём, пока в нашей группе освободится место (потолок может меняться,
            # когда тяжёлые видео начинаются/заканчиваются).
            await self._cond.wait_for(lambda: self._active[category] < self._cap(category))
            self._active[category] += 1

    async def release(self, category: str):
        async with self._cond:
            self._active[category] = max(0, self._active[category] - 1)
            # Будим всех: освобождение тяжёлого видео поднимает потолки остальным.
            self._cond.notify_all()

    def is_full(self, category: str) -> bool:
        return self._active[category] >= self._cap(category)


_limiter = _SmartLimiter()


async def acquire(category: str):
    await _limiter.acquire(category)


async def release(category: str):
    await _limiter.release(category)


def queue_is_full(category: str = LIGHT) -> bool:
    """True, если в группе нет свободных слотов (значит задача встанет в очередь)."""
    return _limiter.is_full(category)


def friendly_error(exc: Exception, lang: str = "ru") -> str:
    """Понятное пользователю сообщение по тексту ошибки (на языке пользователя)."""
    text = str(exc).lower()

    if isinstance(exc, FileTooLargeError) or "too large" in text or "file is too big" in text \
            or "request entity too large" in text or "413" in text:
        return t("err_too_large", lang)
    if "drm" in text:
        return t("err_drm", lang)
    # Instagram: «доступ не для всех / certain audiences» — контент только для вошедших
    # в аккаунт (не 18+). Лечится свежими куками; пользователю — мягкое сообщение.
    if "available to everyone" in text or "certain audiences" in text or "audiences" in text \
            or "login required" in text or "requires login" in text:
        return t("err_login_required", lang)
    if "restricted" in text or "age-restricted" in text:
        return t("err_restricted", lang)
    if "login" in text or "private" in text or "приватн" in text or "not available for guest" in text:
        return t("err_private", lang)
    if "geo" in text or "not available in your country" in text or "geoblock" in text:
        return t("err_geo", lang)
    if "age" in text and "confirm" in text:
        return t("err_age", lang)
    if "unavailable" in text or "not found" in text or "404" in text or "removed" in text:
        return t("err_not_found", lang)
    if "timed out" in text or "timeout" in text or "connection" in text:
        return t("err_network", lang)
    return t("generic_dl_failed", lang)
