"""
Замер скорости скачивания по ссылке. Ручной инструмент, частью бота не является.

Зачем это нужно отдельно от проверки функционала: та отвечает «скачалось ли», но не
«стало ли лучше». Второй вопрос закрывается только числами до и после правки, снятыми
на живой ссылке. Именно так нашлись сломанное сжатие (360p вместо 720p) и отставание
на TikTok.

Раньше это была команда /bench внутри бота. Её убрали: замеры нужны несколько раз в
год, а команда всё остальное время висела в меню и мешала. Логика осталась прежняя,
переехала только точка входа.

Меряем САМО скачивание, без отправки в Telegram: иначе цифра зависела бы от того,
насколько быстро сейчас отвечают серверы Telegram, и сравнивать «до и после» было бы
нельзя. Кэш бота тоже не участвует – качаем всегда заново.

Чего здесь нет и быть не может: сравнения с чужим ботом. Для этого нужен обычный
аккаунт Telegram – см. соседний tools/bench.py.

Запуск на сервере (внутри контейнера, где стоят yt-dlp, ffmpeg и куки):
    docker compose exec bot python tools/speedbench.py <ссылка> [<ссылка> ...]

Локально (из корня проекта, с активированным окружением бота):
    python tools/speedbench.py <ссылка>
"""
import asyncio
import logging
import os
import time
import sys
from functools import partial

# Скрипт лежит в tools/, а импортирует bot.* — добавляем корень проекта в пути.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import SHORTS_CAP_HEIGHT
from bot.utils.platform_detector import detect_platform, Platform

logger = logging.getLogger(__name__)

# Платформы, для которых имеет смысл мерить два режима: у них работает «Сжатие шортс».
_COMPRESSIBLE = (Platform.YOUTUBE_SHORTS, Platform.INSTAGRAM_REEL, Platform.TIKTOK)


def _cleanup(paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def _measure(fn) -> dict:
    """Один замер: время, суммарный размер и разрешение того, что скачалось."""
    from bot.features.download.downloaders.video_meta import probe_video

    start = time.monotonic()
    result = fn()
    sec = round(time.monotonic() - start, 2)

    paths = result if isinstance(result, list) else [result]
    paths = [p for p in paths if p]
    size = sum(os.path.getsize(p) for p in paths if os.path.exists(p))
    meta = probe_video(paths[0]) if paths and os.path.exists(paths[0]) else {}
    w, h = meta.get("width") or 0, meta.get("height") or 0
    _cleanup(paths)
    return {"sec": sec, "mb": round(size / 1024 / 1024, 2),
            "res": f"{w}x{h}" if w and h else "", "files": len(paths)}


def _jobs(url: str, platform) -> list[tuple[str, object]]:
    """Что именно качать для этой ссылки: (подпись режима, функция без аргументов)."""
    from bot.features.download.downloaders.ytdlp_wrapper import download_shorts, download_video
    from bot.features.download.downloaders.instagram import download_reel
    from bot.features.download.downloaders import tiktok

    cap = SHORTS_CAP_HEIGHT
    if platform == Platform.YOUTUBE_SHORTS:
        return [("full", partial(download_shorts, url)),
                ("cap", partial(download_shorts, url, max_height=cap))]
    if platform == Platform.INSTAGRAM_REEL:
        return [("full", partial(download_reel, url)),
                ("cap", partial(download_reel, url, max_height=cap))]
    if platform == Platform.TIKTOK:
        def tt(compress: bool):
            return tiktok.download_from(tiktok.fetch_tiktok(url, not compress), "auto", compress)
        return [("full", partial(tt, False)), ("cap", partial(tt, True))]
    if platform == Platform.PORNHUB_SHORT:
        from urllib.parse import urlparse
        vid = urlparse(url).path.rstrip("/").split("/")[-1]
        std = f"https://www.pornhub.com/view_video.php?viewkey={vid}"
        return [("full", partial(download_shorts, std))]
    if platform in (Platform.YOUTUBE_VIDEO, Platform.PORNHUB):
        return [("full", partial(download_video, url, 720))]
    return []


async def run_bench(url: str) -> list[dict]:
    """Качает ссылку в доступных режимах и возвращает замеры по каждому."""
    platform = detect_platform(url)
    jobs = _jobs(url, platform)
    if not jobs:
        raise ValueError(f"нечего мерить: площадка {platform.value}")

    rows = []
    for mode, fn in jobs:
        try:
            row = await asyncio.to_thread(_measure, fn)
        except Exception as e:
            logger.warning("Замер %s не удался", mode, exc_info=True)
            row = {"error": str(e)[:100]}
        rows.append({"mode": mode, **row})
    return rows


def print_bench(url: str, rows: list[dict]) -> None:
    """Печатает замер в консоль: по строке на режим."""
    print(f"\n{url}")
    for r in rows:
        name = "без сжатия" if r["mode"] == "full" else f"со сжатием ({SHORTS_CAP_HEIGHT}p)"
        if r.get("error"):
            print(f"  [FAIL] {name}: {r['error']}")
            continue
        extra = f" · {r['res']}" if r.get("res") else ""
        files = f" · {r['files']} шт" if r.get("files", 1) > 1 else ""
        print(f"  {name}: {r['sec']}с · {r['mb']}МБ{extra}{files}")


async def _main(urls: list[str]) -> None:
    for url in urls:
        try:
            rows = await run_bench(url)
        except Exception as e:
            print(f"\n{url}\n  [FAIL] {e}")
            continue
        print_bench(url, rows)
    print("\nКэш не участвовал, качалось заново. Отправка в Telegram не учтена.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    asyncio.run(_main(sys.argv[1:]))
