"""
Одноразовые копии файлов кук для yt-dlp.

Зачем. yt-dlp по окончании работы ПИШЕТ файл кук обратно, сохраняя туда то, что
прислал сервер. Площадки в ответ на часть запросов присылают урезанный набор — и
ключ входа (у Instagram это sessionid) затирается тем, что вернул сайт. Со стороны
это выглядит как «куки протухли сами по себе»: файл на месте, размер похожий, а
войти уже нельзя. На этом мы уже обожглись с Instagram.

Поэтому yt-dlp получает КОПИЮ, а оригинал он не видит и испортить не может. Копии
одноразовые, но процесс живёт долго — старые подчищаются здесь же.

НО У GOOGLE ВСЁ НАОБОРОТ. Он не урезает набор, а РОТИРУЕТ его: на каждый запрос
присылает свежий токен взамен старого, и через несколько часов прежний перестаёт
приниматься. Одноразовая копия эти обновления выбрасывает вместе с собой — и куки
«протухают» сами по себе, хотя срок у них до 2027 года. Для таких площадок есть
working() — постоянная рабочая копия, которую yt-dlp обновляет из раза в раз, а
оригинал (тот, что выгрузил человек) остаётся нетронутым про запас.
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


def working(path: str, work_dir: str) -> str | None:
    """Постоянная рабочая копия файла кук — для площадок, которые их РОТИРУЮТ.

    Заводится один раз рядом с оригиналом (имя + «.work»). Дальше yt-dlp пишет в неё
    свежие токены, и они не теряются между запросами. Если человек перевыгрузил
    оригинал (файл стал новее рабочей копии) — начинаем с него заново.
    """
    if not path or not os.path.exists(path):
        return None
    os.makedirs(work_dir, exist_ok=True)
    work = os.path.join(work_dir, os.path.basename(path) + ".work")
    try:
        fresh_original = (not os.path.exists(work)
                          or os.path.getmtime(path) > os.path.getmtime(work))
        if fresh_original:
            shutil.copyfile(path, work)
            logger.info("Куки %s: завёл рабочую копию из свежего оригинала", path)
    except OSError:
        logger.warning("Не смог подготовить рабочую копию кук %s", path, exc_info=True)
        return None
    return work
