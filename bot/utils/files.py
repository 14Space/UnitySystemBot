"""
Уборка временных файлов.

Одна функция вместо семи: своя уборка была у ссылок, у расшифровки, у проверки
функционала, у TikTok, HDRezka, подготовки звука и у сетевого загрузчика – и каждая
чуть по-своему. Одна считала объём для статистики износа диска, другая нет; одна
принимала список, другая только путь, и в finally приходилось помнить, какая где.
"""
import logging
import os

from bot.utils import traffic

logger = logging.getLogger(__name__)


def remove(*paths, record: bool = False) -> None:
    """Удаляет файлы. Принимает пути, списки путей и None – в finally переменная
    попадает в любом состоянии: ещё не заведённая (скачивание упало на первой
    строке), одиночный путь или список файлов поста.

    record=True – учесть размер в статистике трафика (сколько записано на SSD). Так
    делаем для того, что качали ради человека; служебные и промежуточные файлы не
    считаем, как не считали и раньше.
    """
    for item in paths:
        if not item:
            continue
        if isinstance(item, (list, tuple, set)):
            remove(*item, record=record)
            continue
        try:
            if os.path.exists(item):
                if record:
                    traffic.record(item)
                os.remove(item)
        except OSError:
            logger.warning("Не смог удалить %s", item)


# Подписи форматов в первых байтах файла. Расширение нам сообщает площадка, а байты –
# сам файл: вместо картинки по ссылке может приехать страница-заглушка или видео.
_IMAGE_MAGIC = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"),
                (b"GIF8", ".gif"))


def sniff(path: str) -> tuple[str, str] | None:
    """Что это за файл по его содержимому: ("image"|"video", расширение) или None."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    for magic, ext in _IMAGE_MAGIC:
        if head.startswith(magic):
            return "image", ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", ".webp"
    if head[4:8] == b"ftyp":
        # Тот же контейнер у HEIC/AVIF-картинок – различаем по марке после ftyp.
        if head[8:12] in (b"heic", b"heix", b"mif1", b"msf1"):
            return "image", ".heic"
        if head[8:12] in (b"avif", b"avis"):
            return "image", ".avif"
        return "video", ".mp4"                 # mp4 / mov
    if head.startswith(b"\x1a\x45\xdf\xa3"):   # webm / mkv
        return "video", ".webm"
    return None
