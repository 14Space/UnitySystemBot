from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import ADMIN_ID
from bot.database import SessionLocal
from bot.database.repository import get_stats, clear_cache
from bot.utils.i18n import t, lang_of

router = Router()


def format_stats(stats: dict) -> str:
    """Текст отчёта по статистике."""
    lines = ["📊 <b>Статистика viaSaver</b>", ""]
    lines.append(f"👥 Пользователей: <b>{stats['users']}</b>")
    lines.append(f"⬇️ Всего запросов: <b>{stats['total_downloads']}</b>")

    if stats["downloads"]:
        lines.append("\n<b>По платформам:</b>")
        for platform, count in sorted(stats["downloads"].items(), key=lambda x: -x[1]):
            lines.append(f"• {platform}: {count}")

    if stats["languages"]:
        total = sum(stats["languages"].values()) or 1
        lines.append("\n<b>Языки пользователей:</b>")
        for lang, cnt in sorted(stats["languages"].items(), key=lambda x: -x[1]):
            lines.append(f"• {lang}: {cnt} ({cnt * 100 // total}%)")

    return "\n".join(lines)


def _is_admin(message: Message) -> bool:
    return bool(ADMIN_ID) and message.from_user.id == ADMIN_ID


@router.message(Command("statistics", "stats"))
async def cmd_stats(message: Message):
    # Доступно только админу; остальным — тишина
    if not _is_admin(message):
        return
    async with SessionLocal() as session:
        stats = await get_stats(session)
    await message.answer(format_stats(stats), parse_mode="HTML")


@router.message(Command("cleancache"))
async def cmd_cleancache(message: Message):
    """Админ: чистит весь кэш file_id (стираются только ссылки, файлы в Telegram целы)."""
    if not _is_admin(message):
        return
    async with SessionLocal() as session:
        count = await clear_cache(session)
    await message.answer(t("cache_cleared", lang_of(message.from_user), count=count), parse_mode="HTML")
