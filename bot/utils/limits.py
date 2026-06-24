import asyncio
from bot.config import MAX_PARALLEL_DOWNLOADS

# Когда все слоты заняты — новые ждут своей очереди (await acquire).
_download_semaphore = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)

# Предел размера файла (лимит локального Telegram Bot API — 2 ГБ, берём с запасом).
MAX_FILE_BYTES = 1_950_000_000


class FileTooLargeError(Exception):
    """Файл больше лимита Telegram (2 ГБ)."""


async def acquire_download():
    await _download_semaphore.acquire()


def release_download():
    _download_semaphore.release()


def queue_is_full() -> bool:
    """True, если все слоты загрузки заняты (значит пользователь встанет в очередь)."""
    return _download_semaphore.locked()


def friendly_error(exc: Exception) -> str:
    """Понятное пользователю сообщение по тексту ошибки."""
    text = str(exc).lower()

    if isinstance(exc, FileTooLargeError) or "too large" in text or "file is too big" in text \
            or "request entity too large" in text or "413" in text:
        return "🔒 Файл слишком большой (больше 2 ГБ). Выбери качество пониже."
    if "drm" in text:
        return "🔒 Этот трек защищён DRM и недоступен для скачивания."
    if "login" in text or "private" in text or "приватн" in text or "not available for guest" in text:
        return "🔒 Контент приватный или требует входа в аккаунт."
    if "geo" in text or "not available in your country" in text or "geoblock" in text:
        return "🌍 Недоступно в этом регионе (гео-блокировка)."
    if "age" in text and "confirm" in text:
        return "🔞 Контент с возрастным ограничением — скачать не удалось."
    if "unavailable" in text or "not found" in text or "404" in text or "removed" in text:
        return "❌ Контент не найден или был удалён."
    if "timed out" in text or "timeout" in text or "connection" in text:
        return "🌐 Проблема с сетью. Попробуй ещё раз чуть позже."
    return "Не удалось скачать, проверь ссылку или попробуй позже."
