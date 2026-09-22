import asyncio
import logging
import os
import shutil
import time

from bot.utils.i18n import t

logger = logging.getLogger(__name__)

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
TRANSCRIBE_BASE = 2    # расшифровок одновременно — всегда до 2 (видеопамяти хватает,
TRANSCRIBE_BOOST = 2   # модель общая, не дублируется; второму ГС не ждать очереди)

# Сколько задача может держать слот, прежде чем счесть её потерянной. Самое долгое,
# что бывает, – фильм на HDRezka: гигабайты через домашний канал, полчаса с запасом.
# Смысл предела в том, что 23.09.2026 бот замолчал на ссылки НАСОВСЕМ: слот заняли и
# не отпустили (сообщение о прогрессе не отправилось, а освобождение стояло в finally
# ниже по коду), и все лёгкие загрузки встали в очередь за мёртвым держателем. Ждали
# они молча – со стороны бот выглядел живым и просто не отвечал.
MAX_HOLD_SECONDS = int(os.getenv("MAX_HOLD_SECONDS", str(40 * 60)))

# Предел размера файла (лимит локального Telegram Bot API — 2 ГБ, берём с запасом).
MAX_FILE_BYTES = 1_950_000_000


class FileTooLargeError(Exception):
    """Файл больше лимита Telegram (2 ГБ)."""


class NoDiskSpaceError(Exception):
    """На диске не осталось места под загрузку."""


# Сколько места должно оставаться свободным, чтобы браться за тяжёлую загрузку.
# Фильм на HDRezka спокойно весит полтора гигабайта, плюс yt-dlp держит видео и звук
# отдельными файлами и склеивает их третьим — пик занимает примерно вдвое больше
# итогового размера.
MIN_FREE_BYTES = int(os.getenv("MIN_FREE_BYTES", str(4 * 1024 ** 3)))


def free_space(path: str) -> int:
    """Свободно байт на диске, где лежит path (0 — узнать не вышло)."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def check_disk_space(path: str, need: int = MIN_FREE_BYTES) -> None:
    """Бросает NoDiskSpaceError, если места явно не хватит.

    Зачем отдельной проверкой: когда диск забит, yt-dlp и ffmpeg падают с невнятными
    ошибками уровня «errno 28», человек видит «не удалось скачать», а настоящая причина
    остаётся только в логах. Тут мы говорим прямо и заранее — и заодно шлём тревогу
    админу через общий путь friendly_error.
    """
    free = free_space(path)
    if free and free < need:
        raise NoDiskSpaceError(
            f"на диске свободно {free / 1024 ** 3:.1f} ГБ, нужно хотя бы "
            f"{need / 1024 ** 3:.1f} ГБ")


class _SmartLimiter:
    """Считает активные задачи по группам и пускает новые только в пределах
    динамического потолка. Ждущие просыпаются, как только слот освобождается."""

    def __init__(self):
        self._cond = asyncio.Condition()
        self._active = {HEAVY: 0, LIGHT: 0, TRANSCRIBE: 0}
        # Когда начался каждый из занятых слотов. Нужно, чтобы отличить работающую
        # задачу от потерянной: счётчик сам по себе этого не знает (см. _forget_lost).
        self._started: dict[str, list[float]] = {HEAVY: [], LIGHT: [], TRANSCRIBE: []}

    def _forget_lost(self, category: str) -> int:
        """Отпускает слоты, занятые дольше MAX_HOLD_SECONDS. Возвращает их число.

        Это страховка, а не рабочий механизм: если она срабатывает, значит где-то
        потерялось освобождение, и об этом надо узнать из лога, а не по молчанию бота.
        """
        # Сравнение НЕ строгое: «держит ровно столько же» — тоже потерян. У монотонных
        # часов на Windows шаг около 15 мс, и два слота, занятых в один тик, при строгом
        # сравнении не ловились вовсе.
        edge = time.monotonic() - MAX_HOLD_SECONDS
        lost = [s for s in self._started[category] if s <= edge]
        if not lost:
            return 0
        self._started[category] = [s for s in self._started[category] if s > edge]
        self._active[category] = max(0, self._active[category] - len(lost))
        logger.warning("Лимит «%s»: отпустил %d зависших слотов (держали дольше %d мин) "
                       "— значит где-то потерялось освобождение",
                       category, len(lost), MAX_HOLD_SECONDS // 60)
        return len(lost)

    def _cap(self, category: str) -> int:
        heavy_idle = self._active[HEAVY] == 0
        if category == HEAVY:
            return HEAVY_MAX
        if category == LIGHT:
            return LIGHT_BOOST if heavy_idle else LIGHT_BASE
        if category == TRANSCRIBE:
            return TRANSCRIBE_BOOST if heavy_idle else TRANSCRIBE_BASE
        return 0

    def _free(self, category: str) -> bool:
        """Есть ли место. Заодно вычищает зависшие слоты — иначе ждущие висели бы
        вечно, а проверка условия как раз то место, где это заметно."""
        if self._active[category] >= self._cap(category):
            self._forget_lost(category)
        return self._active[category] < self._cap(category)

    async def acquire(self, category: str):
        async with self._cond:
            # Ждём, пока в нашей группе освободится место (потолок может меняться,
            # когда тяжёлые видео начинаются/заканчиваются). Условие проверяется и по
            # будильнику, чтобы зависшие слоты отпускались даже когда никто не
            # освобождается и будить нас некому.
            while not self._free(category):
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=60)
                except asyncio.TimeoutError:
                    continue
            self._active[category] += 1
            self._started[category].append(time.monotonic())

    async def release(self, category: str):
        async with self._cond:
            self._active[category] = max(0, self._active[category] - 1)
            if self._started[category]:
                self._started[category].pop(0)      # ушла самая старая из занятых
            # Будим всех: освобождение тяжёлого видео поднимает потолки остальным.
            self._cond.notify_all()

    def is_full(self, category: str) -> bool:
        return self._active[category] >= self._cap(category)

    def state(self) -> dict:
        """Кто сколько слотов занимает и как долго — для /test и разбора залипаний."""
        now = time.monotonic()
        return {c: {"active": self._active[c], "cap": self._cap(c),
                    "oldest_sec": int(now - min(self._started[c])) if self._started[c] else 0}
                for c in (HEAVY, LIGHT, TRANSCRIBE)}


_limiter = _SmartLimiter()


async def acquire(category: str):
    await _limiter.acquire(category)


async def acquire_or_tell(category: str, message, lang: str = "ru") -> None:
    """Занимает слот, а если ждать приходится долго — говорит об этом человеку.

    Раньше ожидание было молчаливым, и «бот не отвечает» выглядело одинаково и когда
    он занят, и когда сломан. Теперь человек видит, что его услышали.
    """
    if not queue_is_full(category):
        await acquire(category)
        return
    notice = None
    try:
        notice = await message.reply(t("in_queue", lang))
    except Exception:
        pass                       # не вышло предупредить — ждём всё равно
    await acquire(category)
    if notice is not None:
        try:
            await notice.delete()
        except Exception:
            pass


def state() -> dict:
    return _limiter.state()


async def release(category: str):
    await _limiter.release(category)


def queue_is_full(category: str = LIGHT) -> bool:
    """True, если в группе нет свободных слотов (значит задача встанет в очередь)."""
    return _limiter.is_full(category)


# Хук уведомления о сбое: main подключает сюда alerts.note_failure, чтобы КАЖДЫЙ
# показанный пользователю сбой заодно уведомлял админа. Держим необязательным, чтобы
# limits не зависел от бота (и тесты/воркеры работали без него).
_failure_hook = None


def set_failure_hook(hook) -> None:
    global _failure_hook
    _failure_hook = hook


def friendly_error(exc: Exception, lang: str = "ru") -> str:
    """Понятное пользователю сообщение по тексту ошибки (на языке пользователя)."""
    if _failure_hook is not None:
        try:
            _failure_hook(exc)          # уведомить админа (не роняем ответ пользователю)
        except Exception:
            pass
    text = str(exc).lower()

    if isinstance(exc, NoDiskSpaceError) or "no space left" in text or "errno 28" in text:
        return t("err_no_space", lang)
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
    # YouTube пишет это по-разному: «not available in your country» и длиннее —
    # «the uploader has not made this video available in your country». Ловим по
    # общему куску, иначе второй вариант уезжал в «не удалось скачать».
    if "geo" in text or "available in your country" in text or "geoblock" in text:
        return t("err_geo", lang)
    if "age" in text and "confirm" in text:
        return t("err_age", lang)
    if "unavailable" in text or "not found" in text or "404" in text or "removed" in text:
        return t("err_not_found", lang)
    if "timed out" in text or "timeout" in text or "connection" in text:
        return t("err_network", lang)
    return t("generic_dl_failed", lang)
