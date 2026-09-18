"""
Одноразовые копии файлов кук для yt-dlp.

Зачем. yt-dlp по окончании работы ПИШЕТ файл кук обратно, сохраняя туда то, что
прислал сервер. Площадки в ответ на часть запросов присылают урезанный набор — и
ключ входа (у Instagram это sessionid) затирается тем, что вернул сайт. Со стороны
это выглядит как «куки протухли сами по себе»: файл на месте, размер похожий, а
войти уже нельзя. На этом мы уже обожглись с Instagram.

Поэтому yt-dlp получает КОПИЮ, а оригинал он не видит и испортить не может. Копии
одноразовые, но процесс живёт долго — старые подчищаются здесь же.
"""
import logging
import os
import shutil
import threading
import uuid

logger = logging.getLogger(__name__)

_copies: list[str] = []
_lock = threading.Lock()
# Сколько копий держим. Каждая — это мелкий текстовый файл, но без предела они
# копились бы всю жизнь процесса.
_MAX = 20


def disposable(path: str, tmp_dir: str) -> str | None:
    """Копия файла кук, которую не жалко отдать yt-dlp. None, если файла нет."""
    if not path or not os.path.exists(path):
        return None
    try:
        os.makedirs(tmp_dir, exist_ok=True)
        copy = os.path.join(tmp_dir, f"ck_{uuid.uuid4().hex[:8]}.txt")
        shutil.copyfile(path, copy)
    except OSError:
        logger.warning("Не смог сделать копию кук %s — иду без них", path, exc_info=True)
        return None
    with _lock:
        _copies.append(copy)
        while len(_copies) > _MAX:
            old = _copies.pop(0)
            try:
                os.remove(old)
            except OSError:
                pass
    return copy
