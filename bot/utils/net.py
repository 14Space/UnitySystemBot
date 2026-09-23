"""
Скачивание файла НА ДИСК, а не в память.

Зачем. По всему проекту files качались одинаково: `requests.get(...).content`, то есть
файл целиком оказывался в оперативной памяти, и только потом записывался на диск.
Для картинки это неважно, а для видео — уже нет: у бота 7 ГБ на всё, лёгких загрузок
одновременно шесть, и каждая держала свой файл в памяти дважды (байты плюс запись).
Внешний аудит нашёл десяток таких мест. Заодно там не было предела размера: сколько
площадка отдала, столько и приняли — а Telegram больше 2 ГБ всё равно не пропустит.

Здесь один загрузчик на всех: читает поток кусками, пишет сразу в файл, обрывает
скачивание при превышении предела и убирает за собой недокачанное.
"""
import logging
import os

import requests

from bot.utils.limits import FileTooLargeError, MAX_FILE_BYTES

logger = logging.getLogger(__name__)

# Размер куска. 256 КБ — обычный компромисс: системных вызовов немного, а памяти
# держим на порядки меньше, чем весит сам файл.
_CHUNK = 256 * 1024


def fetch_to_file(url: str, path: str, *, max_bytes: int = MAX_FILE_BYTES,
                  headers: dict | None = None, proxies: dict | None = None,
                  timeout: int = 60, session: requests.Session | None = None) -> str:
    """Качает url в файл path и возвращает путь.

    Бросает FileTooLargeError, если файл больше max_bytes — ПО ХОДУ скачивания, а не
    после: смысл предела в том, чтобы не тратить час и гигабайты канала на файл,
    который всё равно не отправить. Недокачанное удаляем, чтобы на диске не оставалось
    обрубков, которые потом выглядят как «битый файл».
    """
    getter = session.get if session is not None else requests.get
    written = 0
    try:
        with getter(url, headers=headers, proxies=proxies, timeout=timeout,
                    stream=True) as r:
            r.raise_for_status()
            # Если сервер честно сказал размер — отказываемся сразу, не качая.
            declared = int(r.headers.get("Content-Length") or 0)
            if max_bytes and declared > max_bytes:
                raise FileTooLargeError(
                    f"{declared / 1024 ** 3:.1f} ГБ — больше предела "
                    f"{max_bytes / 1024 ** 3:.1f} ГБ")
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                for chunk in r.iter_content(_CHUNK):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if max_bytes and written > max_bytes:
                        raise FileTooLargeError(
                            f"больше предела {max_bytes / 1024 ** 3:.1f} ГБ")
                    f.write(chunk)
    except Exception:
        _drop(path)
        raise
    if written == 0:
        _drop(path)
        raise RuntimeError(f"пустой ответ: {url[:80]}")
    return path


def _drop(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
