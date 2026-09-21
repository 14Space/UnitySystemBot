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

И ГЛАВНОЕ: ротацией дело не ограничивается. 21.09.2026 в 12:00 проверка была
зелёной, в 14:00 — «сессия протухла», причём сам аккаунт оказался жив: из рабочей
копии пропали SID, HSID, SSID, APISID, SAPISID, LOGIN_INFO и вся ветка 1P, а
остался набор незалогиненного гостя вместе с SOCS (кука страницы согласия). То есть
какой-то запрос вернулся «разлогиненным», и yt-dlp записал ЭТО поверх рабочей копии.
Раз так бывает, рабочая копия чинится перед каждой выдачей: чего в ней недостаёт
против оригинала — возвращаем, а обновлённое Google оставляем как есть. Само
«разлогинивание» этим не лечится, но сессию оно больше не уносит.
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


def _rows(path: str) -> dict[tuple[str, str, str], str]:
    """Куки из файла формата Netscape: {(домен, путь, имя): строка целиком}.

    Строка кук — это домен, флаг, путь, secure, срок, ИМЯ, значение через табуляцию.
    Шапка файла и всё, что на строку кук не похоже, пропускается.
    """
    rows: dict[tuple[str, str, str], str] = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 7:
                continue
            rows[(parts[0], parts[2], parts[5])] = line.rstrip("\n")
    return rows


def _repair(original: str, work: str) -> None:
    """Возвращает в рабочую копию куки, которые из неё пропали.

    Значения, которые Google успел обновить, не трогаем — ради них рабочая копия и
    заведена. Дописываем только то, чего в ней не стало вовсе: именно так уходит
    ключ входа, когда какой-то запрос вернулся «разлогиненным».
    """
    src, dst = _rows(original), _rows(work)
    lost = {key: line for key, line in src.items() if key not in dst}
    if not lost:
        return
    with open(work, encoding="utf-8", errors="replace") as f:
        body = f.read()
    if body and not body.endswith("\n"):
        body += "\n"
    tmp = work + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(body + "\n".join(lost.values()) + "\n")
    os.replace(tmp, work)      # подмена целиком: недописанного файла никто не увидит
    logger.warning("Куки %s: из рабочей копии пропало %d штук (%s) — вернул из оригинала",
                   original, len(lost),
                   ", ".join(sorted(name for _, _, name in lost))[:120])


def working(path: str, work_dir: str) -> str | None:
    """Постоянная рабочая копия файла кук — для площадок, которые их РОТИРУЮТ.

    Заводится один раз рядом с оригиналом (имя + «.work») — именно рядом, а не в
    папке загрузок: ту чистят при каждом старте, и вместе с хвостами улетала вся
    накопленная ротация, так что после любого деплоя мы шли с выгрузки многодневной
    давности. Если человек перевыгрузил оригинал (файл стал новее рабочей копии) —
    начинаем с него заново.

    Перед каждой выдачей копия чинится, см. _repair.
    """
    if not path or not os.path.exists(path):
        return None
    os.makedirs(work_dir, exist_ok=True)
    work = os.path.join(work_dir, os.path.basename(path) + ".work")
    try:
        with _lock:            # заводим и чиним по одному: копия у площадки общая
            fresh_original = (not os.path.exists(work)
                              or os.path.getmtime(path) > os.path.getmtime(work))
            if fresh_original:
                shutil.copyfile(path, work)
                logger.info("Куки %s: завёл рабочую копию из свежего оригинала", path)
            else:
                _repair(path, work)
    except OSError:
        logger.warning("Не смог подготовить рабочую копию кук %s", path, exc_info=True)
        return None
    return work
