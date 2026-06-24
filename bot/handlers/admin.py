from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import ADMIN_ID
from bot.database import SessionLocal
from bot.database.repository import get_stats

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


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    # Доступно только админу; остальным — тишина
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    async with SessionLocal() as session:
        stats = await get_stats(session)
    await message.answer(format_stats(stats), parse_mode="HTML")
