from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import ADMIN_ID
from bot.database import SessionLocal
from bot.database.repository import get_stats, clear_cache
from bot.utils.i18n import t, lang_of

router = Router()

# Подразделы площадок сводим в одну платформу для статистики (например, instagram_reel
# и instagram_post → Instagram). YouTube и YT Music держим раздельно — это по сути два
# разных сервиса. Ключи слева — как их пишет БД (см. platform_detector).
PLATFORM_GROUP = {
    "tiktok": "TikTok",
    "instagram_reel": "Instagram", "instagram_post": "Instagram",
    "hdrezka": "HDRezka",
    "twitter": "Twitter",
    "youtube_video": "YouTube", "youtube_shorts": "YouTube",
    "yt_music": "YT Music",
    "spotify": "Spotify", "spotify_collection": "Spotify",
    "pinterest": "Pinterest",
    "pornhub": "PornHub", "pornhub_short": "PornHub",
    "soundcloud": "SoundCloud",
}


def _grouped_downloads(downloads: dict) -> list[tuple[str, int]]:
    """Сводит подразделы в платформы и сортирует по убыванию запросов."""
    agg: dict[str, int] = {}
    for key, cnt in (downloads or {}).items():
        name = PLATFORM_GROUP.get(key, key)
        agg[name] = agg.get(name, 0) + cnt
    return sorted(agg.items(), key=lambda x: -x[1])


def platform_ranking(stats: dict) -> list[str]:
    """Платформы по убыванию использования — для порядка в проверке функционала."""
    return [name for name, _ in _grouped_downloads(stats.get("downloads") or {})]


def format_stats(stats: dict, lang: str = "ru") -> str:
    """Текст отчёта по статистике на языке админа."""
    lines = [t("rep_title", lang), ""]
    lines.append(f"{t('rep_users', lang)}: <b>{stats['users']}</b>")
    lines.append(f"{t('rep_requests', lang)}: <b>{stats['total_downloads']}</b>")

    if stats["downloads"]:
        lines.append(f"\n<b>{t('rep_platforms', lang)}</b>")
        for platform, count in _grouped_downloads(stats["downloads"]):
            lines.append(f"• {platform}: {count}")

    if stats["languages"]:
        total = sum(stats["languages"].values()) or 1
        lines.append(f"\n<b>{t('rep_langs', lang)}</b>")
        for code, cnt in sorted(stats["languages"].items(), key=lambda x: -x[1]):
            lines.append(f"• {code}: {cnt} ({cnt * 100 // total}%)")

    if stats.get("traffic"):
        lines.append("\n" + t("rep_traffic", lang))
        for row in stats["traffic"]:
            lines.append(f"• {row['month']}: {_human_size(row['bytes'], lang)}"
                         f" ({row['files']} {t('rep_files', lang)})")

    return "\n".join(lines)


def _human_size(nbytes: int, lang: str = "ru") -> str:
    """Байты в человекочитаемый вид: 1.4 ГБ, 812 МБ и т.п."""
    size = float(nbytes)
    for key in ("size_b", "size_kb", "size_mb", "size_gb", "size_tb"):
        unit = t(key, lang)
        if size < 1024 or key == "size_tb":
            return f"{size:.0f} {unit}" if key in ("size_b", "size_kb") else f"{size:.1f} {unit}"
        size /= 1024


def _is_admin(message: Message) -> bool:
    return bool(ADMIN_ID) and message.from_user.id == ADMIN_ID


@router.message(Command("statistics", "stats"))
async def cmd_stats(message: Message):
    # Доступно только админу; остальным — тишина
    if not _is_admin(message):
        return
    from bot.features.common.healthcheck import last_results, run_and_cache, format_health
    async with SessionLocal() as session:
        stats = await get_stats(session)
    # Проверку не гоняем заново на каждое нажатие — берём последнюю (её снимают каждые
    # несколько часов и в полдень). Если кэша ещё нет (бот только запустился) — снимем.
    results, at = last_results()
    if not results:
        results = await run_and_cache()
        _, at = last_results()
    lang = lang_of(message.from_user)
    report = (f"{format_stats(stats, lang)}\n\n"
              f"{format_health(results, platform_ranking(stats), at=at, lang=lang)}")
    await message.answer(report, parse_mode="HTML")


@router.message(Command("cleancache"))
async def cmd_cleancache(message: Message):
    """Админ: чистит кэш file_id этого бота (стираются только ссылки, файлы в Telegram целы)."""
    if not _is_admin(message):
        return
    async with SessionLocal() as session:
        count = await clear_cache(session)
    await message.answer(t("cache_cleared", lang_of(message.from_user), count=count), parse_mode="HTML")


@router.message(Command("bench"))
async def cmd_bench(message: Message):
    """Админ: замеряет скорость скачивания по ссылке – отдельно без сжатия и со сжатием.

    Зачем: проверка функционала отвечает «скачалось ли», но не «стало ли быстрее».
    Этой командой снимаются цифры ДО правки и ПОСЛЕ, чтобы сравнивать по числам, а не
    по ощущениям – именно так нашлись и сломанное сжатие, и отставание на TikTok.

    Меряется САМО скачивание, без отправки в Telegram: так цифры не зависят от того,
    насколько быстро сейчас работает связь с серверами Telegram.

    Сравнить себя с чужим ботом отсюда нельзя: в Telegram бот не может писать боту.
    Для этого нужен обычный аккаунт (см. tools/bench.py).
    """
    if not _is_admin(message):
        return
    from bot.features.common.benchmark import run_bench, format_bench

    parts = (message.text or "").split(maxsplit=1)
    url = parts[1].strip() if len(parts) > 1 else ""
    lang = lang_of(message.from_user)
    if not url:
        await message.answer(t("bench_usage", lang))
        return

    status = await message.answer(t("bench_running", lang))
    try:
        rows = await run_bench(url)
    except Exception as e:
        await status.edit_text(t("bench_failed", lang, reason=str(e)[:120]))
        return
    await status.edit_text(format_bench(url, rows, lang), parse_mode="HTML")
