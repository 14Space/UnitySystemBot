"""
ВРЕМЕННО – до мини-ПК. Жив ли домашний туннель (ноутбук владельца).

Сейчас «домом» для бота служит ноутбук: выключен он – туннеля нет, и всё, что шло
через дом, ждало по 60 секунд до обрыва, после чего человеку уходила ошибка, а
админу – тревога. Здесь бот раз в 30 секунд сам проверяет туннель и, пока его нет:
  • идёт напрямую с адреса сервера, а не ждёт мёртвый прокси (net.with_proxy);
  • НЕ берёт куки Instagram (им можно только через дом, см. instagram._attempts);
  • при неудаче молчит – ни ответа человеку, ни тревоги админу (job.produce, alerts);
  • пропускает проверку площадок по расписанию (main.py).
Так решил владелец (29.09.2026): пока ноутбук выключен, бот просто не отвечает.

КАК ВЫРЕЗАТЬ, когда дома встанет мини-ПК: удалить этот файл и тест
tests/test_home_tunnel.py, затем убрать каждую строку, найденную поиском
«ВРЕМЕННО home_tunnel» – все подключения помечены так и больше ничем не связаны.
"""
import asyncio
import logging
import socket
import time
from urllib.parse import urlparse

from bot.utils import net

logger = logging.getLogger(__name__)

# Как часто проверяем. Ноутбук включился – через полминуты бот снова ходит через дом.
_EVERY = 30
# Сколько ждать ответа прокси. Живой туннель отвечает за доли секунды.
_TIMEOUT = 5

# None – ещё не проверяли: считаем туннель живым, чтобы до первой проверки вести
# себя как раньше.
_down: bool | None = None
_since = time.monotonic()


def _proxy() -> str:
    return net.proxy_for() or net.proxy_for("instagram")


def _probe_sync(proxy: str) -> bool:
    """Туннель жив, если прокси на том конце ответил на приветствие SOCKS5."""
    u = urlparse(proxy)
    try:
        with socket.create_connection((u.hostname, u.port or 1080), timeout=_TIMEOUT) as s:
            s.settimeout(_TIMEOUT)
            s.sendall(b"\x05\x01\x00")            # SOCKS5, один способ входа – без пароля
            return s.recv(2) == b"\x05\x00"
    except OSError:
        return False


def _set(down: bool) -> None:
    global _down, _since
    if down != _down:
        if _down is not None:
            logger.info("Домашний туннель %s (было %d мин)",
                        "пропал" if down else "снова работает",
                        (time.monotonic() - _since) // 60)
        _down, _since = down, time.monotonic()


def is_down() -> bool:
    """Туннель настроен, но не отвечает (по последней проверке)."""
    return bool(_down) and bool(_proxy())


def usable(proxy: str) -> str:
    """Прокси, если через него сейчас можно ходить, иначе «» – напрямую."""
    return "" if proxy and is_down() else proxy


async def check_now() -> bool:
    """Проверить сейчас (а не ждать очередного круга). True – туннеля нет."""
    proxy = _proxy()
    if not proxy:
        return False
    _set(not await asyncio.to_thread(_probe_sync, proxy))
    return bool(_down)


async def watch() -> None:
    """Фоновая задача: проверка раз в _EVERY секунд."""
    if not _proxy():
        return
    while True:
        try:
            await check_now()
        except Exception:
            logger.warning("Проверка домашнего туннеля упала", exc_info=True)
        await asyncio.sleep(_EVERY)
