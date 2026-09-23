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
