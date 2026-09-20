from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import ADMIN_ID
from bot.database import SessionLocal
from bot.database.repository import get_stats, clear_cache
from bot.utils.i18n import t, lang_of

router = Router()

# Подразделы площадок сводим в одну платформу для статистики (например, instagram_reel
# и instagram_post → Instagram). YouTube и YouTube Music держим раздельно — это по сути два
# разных сервиса. Ключи слева — как их пишет БД (см. platform_detector).
PLATFORM_GROUP = {
    "tiktok": "TikTok",
    "instagram_reel": "Instagram", "instagram_post": "Instagram",
    "hdrezka": "HDRezka",
    "twitter": "Twitter",
    "youtube_video": "YouTube", "youtube_shorts": "YouTube",
    # Пишем ровно так же, как называется пункт в проверке функционала: одно и то же
    # в двух списках одного отчёта не должно называться по-разному.
    "yt_music": "YouTube Music",
    "spotify": "Spotify", "spotify_collection": "Spotify",
    "pinterest": "Pinterest",
    "pornhub": "PornHub", "pornhub_short": "PornHub",
    "soundcloud": "SoundCloud",
}


def _grouped_downloads(downloads: dict) -> list[tuple[str, int]]:
    """Сводит подразделы в платформы и сортирует по убыванию запросов.

    Площадки с нулём тоже показываем. Раньше их просто не было в списке, и «Pinterest
    не пользуются» выглядело неотличимо от «Pinterest сломался и ссылки перестали
    распознаваться». Ноль — это тоже информация.
    """
    agg: dict[str, int] = {name: 0 for name in PLATFORM_GROUP.values()}
    for key, cnt in (downloads or {}).items():
        name = PLATFORM_GROUP.get(key, key)
        agg[name] = agg.get(name, 0) + cnt
    # При равном числе — по алфавиту, иначе нулевые площадки скакали бы между отчётами.
    return sorted(agg.items(), key=lambda x: (-x[1], x[0]))


def platform_ranking(stats: dict) -> list[str]:
    """Платформы по убыванию использования — для порядка в проверке функционала."""
    return [name for name, _ in _grouped_downloads(stats.get("downloads") or {})]


def format_stats(stats: dict, lang: str = "ru") -> str:
    """Текст отчёта по статистике на языке админа."""
    lines = [t("rep_title", lang), ""]
    lines.append(f"{t('rep_users', lang)}: <b>{stats['users']}</b>")
    # «Всего» — цифра историческая: в ней и те, кто когда-то просто оказался в группе с
    # ботом. Живую картину показывает вторая строка.
    lines.append(f"{t('rep_active', lang)}: <b>{stats.get('active', 0)}</b>")
    lines.append(f"{t('rep_premium', lang)}: <b>{stats.get('premium', 0)}</b>")

    # Порядок блоков: сперва про людей (сколько их и на каких языках), потом про
    # запросы. Языки короткие, их не прячем; список площадок бывает длинным — он
    # уезжает в раскрывающуюся цитату, чтобы отчёт читался с одного экрана.
    if stats["languages"]:
        total = sum(stats["languages"].values()) or 1
        lines.append(t("rep_langs", lang))
        for code, cnt in sorted(stats["languages"].items(), key=lambda x: -x[1]):
            lines.append(f"• {code}: {cnt} ({cnt * 100 // total}%)")

    # Покупки показываем, только когда они есть: у бота без единой продажи строка
    # «Покупок: 0» каждый день — лишний шум в отчёте.
    pay = stats.get("payments") or {}
    if pay.get("count") or pay.get("refunded"):
        row = (f"\n{t('rep_payments', lang)}: <b>{pay['count']}</b>"
               f" ({pay['stars']} {t('rep_stars', lang)})")
        if pay.get("refunded"):
            row += f", {pay['refunded']} {t('rep_refunded', lang)}"
        lines.append(row)

    lines.append(f"\n{t('rep_requests', lang)}: <b>{stats['total_downloads']}</b>")

    if stats["downloads"]:
        lines.append(t("rep_platforms", lang))
        # Тег приклеиваем к первой и последней строке, а не кладём отдельными
        # элементами: иначе после join внутри цитаты появляются пустые строки.
        rows = [f"• {p}: {c}" for p, c in _grouped_downloads(stats["downloads"])]
        rows[0] = "<blockquote expandable>" + rows[0]
        rows[-1] = rows[-1] + "</blockquote>"
        lines += rows

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
    from bot.features.common.healthcheck import last_results, format_health, is_running
    async with SessionLocal() as session:
        stats = await get_stats(session)
    lang = lang_of(message.from_user)
    # Проверку тут не гоняем ВООБЩЕ — показываем последнюю снятую (их снимают каждые
    # несколько часов и в полдень). Раньше при пустом кэше команда запускала полную
    # проверку прямо здесь и молчала минутами, из-за чего бот выглядел зависшим. Пусто
    # бывает первые пару минут после перезапуска и до конца первого прогона — в этом
    # окне отвечаем сразу цифрами и говорим, что проверки пока нет.
    results, at = last_results()
    if results:
        health = format_health(results, platform_ranking(stats), at=at, lang=lang)
    else:
        health = t("health_running" if is_running() else "health_none", lang)
    await message.answer(f"{format_stats(stats, lang)}\n\n{health}", parse_mode="HTML")


@router.message(Command("test"))
async def cmd_test(message: Message):
    """Админ: принудительно прогоняет проверку функционала заново, не глядя на кэш
    (в отличие от /statistics, которая между делом просто показывает последний
    сохранённый результат). Нужна, чтобы проверить систему прямо сейчас — например,
    сразу после переезда на новый сервер или правки в коде."""
    if not _is_admin(message):
        return
    from bot.features.common.healthcheck import run_and_cache, format_health, last_results
    lang = lang_of(message.from_user)
    # Одно сообщение на всю команду: сначала «выполняется», потом тот же текст заменяем
    # результатом. Так в чате не остаётся мусора и не нужно ничего удалять.
    status = await message.answer(t("health_running", lang))
    results = await run_and_cache()
    _, at = last_results()
    report = format_health(results, at=at, lang=lang)
    try:
        await status.edit_text(report, parse_mode="HTML")
    except Exception:
        # Telegram не даёт редактировать сообщение длиннее 4096 символов и отклоняет
        # правку, если текст не изменился. Ни то, ни другое не повод терять отчёт.
        await message.answer(report, parse_mode="HTML")


@router.message(Command("cleancache"))
async def cmd_cleancache(message: Message):
    """Админ: чистит кэш file_id этого бота (стираются только ссылки, файлы в Telegram целы)."""
    if not _is_admin(message):
        return
    async with SessionLocal() as session:
        count = await clear_cache(session)
    await message.answer(t("cache_cleared", lang_of(message.from_user), count=count), parse_mode="HTML")
