"""
ffmpeg и ffprobe: где лежат и как их запускать.

Зачем одним модулем. Путь к ffmpeg искали в пяти местах (скачивание, TikTok, HDRezka,
превью видео, подготовка голосовых), каждое своей строчкой, а предел времени на
запуск был то общий, то свой, то забытый. Зависший процесс (битый файл, обрыв потока,
невнятный кодек) держал бы поток из пула и слот очереди до самого сторожа зависаний,
то есть сорок минут – и всё это молча.

Теперь путь ищется один раз, а запуск идёт через run(): предел времени у него есть
всегда, и забыть его нельзя.
"""
import glob
import os
import shutil
import subprocess

from bot.config import FFMPEG_TIMEOUT, FFPROBE_TIMEOUT


def _find_dir() -> str | None:
    """Папка с ffmpeg: из PATH, а на Windows ещё и там, куда его ставит winget."""
    found = shutil.which("ffmpeg")
    if found:
        return os.path.dirname(found)
    pattern = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*\bin\ffmpeg.exe")
    matches = glob.glob(pattern)
    if matches:
        return os.path.dirname(matches[0])
    return None


# None – не нашли; тогда зовём по имени и надеемся на PATH.
FFMPEG_DIR = _find_dir()


def tool(name: str = "ffmpeg") -> str:
    """Полный путь к ffmpeg/ffprobe, если знаем папку, иначе имя из PATH."""
    return os.path.join(FFMPEG_DIR, name) if FFMPEG_DIR else name


def run(args: list[str], *, timeout: int | None = None,
        text: bool = False) -> subprocess.CompletedProcess:
    """Запускает ffmpeg или ffprobe: args[0] – имя программы, дальше её параметры.

    Вывод собираем (capture_output), ошибку по коду возврата не бросаем – вызывающий
    сам смотрит returncode и файл на выходе. Предел времени по умолчанию зависит от
    программы: ffprobe читает метаданные за доли секунды, ffmpeg перекодирует фильмы.
    """
    name, *rest = args
    if timeout is None:
        timeout = FFPROBE_TIMEOUT if name == "ffprobe" else FFMPEG_TIMEOUT
    return subprocess.run([tool(name), *rest], capture_output=True, text=text,
                          timeout=timeout)
