"""
Метаданные видеофайла для корректной отправки в Telegram.

Зачем: если в send_video не передать width/height/thumbnail, клиент (особенно iOS)
не знает соотношение сторон и рисует «сплюснутое» превью, на котором плеер виснет,
пока не докачается весь файл. Достаём реальные размеры через ffprobe и генерируем
постер-миниатюру через ffmpeg — тогда видео сразу показывается правильно и стримится.
"""
import json
import logging
import os
import subprocess

from bot.features.download.downloaders.ytdlp_wrapper import FFMPEG_DIR

logger = logging.getLogger(__name__)


def _tool(name: str) -> str:
    """Полный путь к ffmpeg/ffprobe, если знаем папку, иначе имя из PATH."""
    return os.path.join(FFMPEG_DIR, name) if FFMPEG_DIR else name


def probe_video(path: str) -> dict:
    """
    Возвращает {'width', 'height', 'duration'} видеофайла (0, если не удалось).
    Использует ffprobe — он есть рядом с ffmpeg.
    """
    result = {"width": 0, "height": 0, "duration": 0}
    try:
        proc = subprocess.run(
            [
                _tool("ffprobe"), "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json", path,
            ],
            capture_output=True, text=True,
        )
        data = json.loads(proc.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        result["width"] = int(stream.get("width") or 0)
        result["height"] = int(stream.get("height") or 0)
        result["duration"] = int(float(data.get("format", {}).get("duration") or 0))
    except Exception:
        logger.warning("ffprobe не смог прочитать %s", path, exc_info=True)
    return result


def make_video_thumbnail(path: str, max_size: int = 320) -> bytes | None:
    """
    Кадр-постер из первой секунды видео как JPEG (≤max_size по большей стороне).
    Telegram использует его как превью, пока видео не подгрузилось.
    """
    try:
        scale = f"scale='if(gt(iw,ih),{max_size},-2)':'if(gt(iw,ih),-2,{max_size})'"
        proc = subprocess.run(
            [
                _tool("ffmpeg"), "-v", "error",
                "-ss", "1", "-i", path,
                "-frames:v", "1", "-vf", scale,
                "-f", "image2", "-vcodec", "mjpeg", "-",
            ],
            capture_output=True,
        )
        data = proc.stdout
        return data if data else None
    except Exception:
        logger.warning("ffmpeg не смог сделать превью для %s", path, exc_info=True)
        return None
