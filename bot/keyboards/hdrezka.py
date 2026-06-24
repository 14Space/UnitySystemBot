import re
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

PAGE_SIZE = 8
FREE_LIMIT = 720  # выше — премиум-заглушка


def build_translator_keyboard(sid: str, translators: list, page: int) -> InlineKeyboardMarkup:
    """Список озвучек кнопками, по 8 на страницу."""
    total_pages = (len(translators) + PAGE_SIZE - 1) // PAGE_SIZE
    start = page * PAGE_SIZE
    chunk = translators[start:start + PAGE_SIZE]

    rows = []
    for tid, name in chunk:
        label = name if len(name) <= 40 else name[:39] + "…"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"hrt:{sid}:{tid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"hrp:{sid}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд →", callback_data=f"hrp:{sid}:{page + 1}"))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_subtitle_keyboard(sid: str, tid: int, subtitles: list) -> InlineKeyboardMarkup:
    """Выбор языка субтитров (+ вариант без них)."""
    rows = [
        [InlineKeyboardButton(text=f"💬 {title}", callback_data=f"hrs:{sid}:{tid}:{code}")]
        for code, title in subtitles
    ]
    rows.append([InlineKeyboardButton(text="Без субтитров", callback_data=f"hrs:{sid}:{tid}:none")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_hdrezka_quality_keyboard(sid: str, tid: int, qualities: list) -> InlineKeyboardMarkup:
    """Качества кнопками. Выше 720p — заблокировано (🔒), как у YouTube."""
    rows = []
    for i, q in enumerate(qualities):
        # 2K/4K — это всегда выше 720p; иначе берём число (1080p -> 1080)
        if "K" in q.upper():
            height = 9999
        else:
            m = re.search(r"(\d+)", q)
            height = int(m.group(1)) if m else 0
        if height > FREE_LIMIT:
            rows.append([InlineKeyboardButton(text=f"🔒 {q}", callback_data="stub:premium_quality")])
        else:
            rows.append([InlineKeyboardButton(text=q, callback_data=f"hrq:{sid}:{tid}:{i}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
