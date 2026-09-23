"""
Мгновенные уведомления админу о КАЖДОМ сбое, который бот показал пользователю
(«не удалось скачать» и т.п.). Работает из одной общей точки — limits.friendly_error
(через хук set_failure_hook), поэтому ловит все места, где бот отвечает ошибкой, без
правки каждого обработчика. Контекст (какая ссылка/платформа) берём из contextvars,
который выставляет обработчик ссылок (process_link).
"""
import asyncio
import contextvars
import logging

from bot.utils.secrets_filter import mask

logger = logging.getLogger(__name__)

# Что бот обрабатывает прямо сейчас (например, "tiktok: https://…") — для контекста
# в уведомлении. Дочерние задачи наследуют значение, выставленное до их создания.
current_request: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_request", default="")

_bot = None
_admin_id = 0
# Тревоги, которые отправляются прямо сейчас. Набор нужен только ради сильной ссылки
# на задачу — см. note_failure.
_PENDING: set = set()


def configure(bot, admin_id: int) -> None:
    """Один раз при старте: кому и от чьего имени слать тревоги о сбоях."""
    global _bot, _admin_id
    _bot, _admin_id = bot, int(admin_id or 0)


def note_failure(exc: Exception) -> None:
    """Синхронный хук (его дёргает friendly_error на потоке event loop): планирует
    отправку админу короткого сообщения о сбое. Если не настроено или мы не в event
    loop — тихо ничего не делаем (сбой пользователю всё равно покажется как раньше)."""
    if not _bot or not _admin_id:
        return
    ctx = current_request.get() or "—"
    # Маскируем: в тексте исключения попадается адрес запроса с токеном или ключом.
    text = mask(f"⚠️ Сбой у пользователя\n{ctx}\n{type(exc).__name__}: {exc}")[:600]
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    # Ссылку на задачу ДЕРЖИМ: asyncio хранит только слабую, и тревога могла быть
    # выброшена сборщиком мусора, так и не уйдя админу.
    task = loop.create_task(_send(text))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def _send(text: str) -> None:
    try:
        await _bot.send_message(_admin_id, text)
    except Exception:
        logger.exception("Не удалось отправить тревогу о сбое")
