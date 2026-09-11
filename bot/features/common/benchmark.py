"""
Замер скорости скачивания по одной ссылке (команда /bench, только админ).

Зачем это нужно отдельно от проверки функционала: та отвечает «скачалось ли», но не
«стало ли лучше». Второй вопрос закрывается только числами до и после правки, снятыми
на живой ссылке. Именно так нашлись сломанное сжатие (360p вместо 720p) и отставание
на TikTok.

Меряем САМО скачивание, без отправки в Telegram: иначе цифра зависела бы от того,
насколько быстро сейчас отвечают серверы Telegram, и сравнивать «до и после» было бы
нельзя. Кэш бота тоже не участвует – качаем всегда заново.

Чего здесь нет и быть не может: сравнения с чужим ботом. В Telegram бот не может
написать боту, для этого нужен обычный аккаунт (см. tools/bench.py).
"""
import asyncio
import logging
import os
import time
from functools import partial

from bot.config import SHORTS_CAP_HEIGHT
from bot.utils.i18n import t
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


def format_bench(url: str, rows: list[dict], lang: str) -> str:
    """Результат замера человеку: по строке на режим."""
    lines = [t("bench_title", lang), f"<code>{url[:80]}</code>", ""]
    for r in rows:
        name = t("bench_full", lang) if r["mode"] == "full" else t(
            "bench_cap", lang, cap=SHORTS_CAP_HEIGHT)
        if r.get("error"):
            lines.append(f"❌ {name}: {r['error']}")
            continue
        extra = f" · {r['res']}" if r.get("res") else ""
        files = f" · {r['files']} шт" if r.get("files", 1) > 1 else ""
        lines.append(f"• {name}: <b>{r['sec']}с</b> · {r['mb']}МБ{extra}{files}")
    lines.append("")
    lines.append(t("bench_note", lang))
    return "\n".join(lines)
