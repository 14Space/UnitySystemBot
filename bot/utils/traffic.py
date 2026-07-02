"""
Учёт скачанного «на диск» контента для оценки износа SSD.

Считаем байты в момент удаления временного файла (_cleanup) — это ровно то, что
было записано на диск. Копим в памяти (быстро, без обращения к БД на каждый файл),
а фоновой задачей раз в минуту сбрасываем накопленное в таблицу monthly_traffic.
"""
import os
import threading

_lock = threading.Lock()
_bytes = 0
_files = 0


def record(path: str):
    """Прибавляет размер файла к накопителю (вызывать перед удалением файла)."""
    global _bytes, _files
    try:
        n = os.path.getsize(path)
    except OSError:
        return
    with _lock:
        _bytes += n
        _files += 1


def take() -> tuple[int, int]:
    """Забирает накопленное (байты, файлы) и обнуляет счётчик — для сброса в БД."""
    global _bytes, _files
    with _lock:
        b, f = _bytes, _files
        _bytes = 0
        _files = 0
    return b, f
