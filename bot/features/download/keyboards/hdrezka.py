import re
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot.utils.i18n import t

from bot.config import FREE_QUALITY_LIMIT

PAGE_SIZE = 8
# Качества, которые сам HDRezka отдаёт только по своей подписке: показывать их нельзя,
# скачать всё равно не получится. Константа жила между двумя функциями и пропала вместе
# с уборкой мёртвого кода — из-за этого клавиатура качеств падала бы при показе.
_HDREZKA_PREMIUM_Q = ("ultra", "2k", "4k")


def build_season_keyboard(sid: str, seasons: list, lang: str = "ru") -> InlineKeyboardMarkup:
    """Сезоны кнопками (по 3 в ряд)."""
    rows, row = [], []
    for s in seasons:
        row.append(InlineKeyboardButton(text=t("btn_season", lang, n=s), callback_data=f"hrss:{sid}:{s}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_episode_keyboard(sid: str, season: int, episodes: list, lang: str = "ru") -> InlineKeyboardMarkup:
    """Серии кнопками (по 5 в ряд) + кнопка назад к сезонам."""
    rows, row = [], []
    for e in episodes:
        row.append(InlineKeyboardButton(text=str(e), callback_data=f"hrep:{sid}:{season}:{e}"))
        if len(row) == 5:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=t("btn_back_to_seasons", lang), callback_data=f"hrback:{sid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_translator_keyboard(sid: str, translators: list, page: int, lang: str = "ru") -> InlineKeyboardMarkup:
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
        nav.append(InlineKeyboardButton(text=t("nav_back", lang), callback_data=f"hrp:{sid}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text=t("nav_forward", lang), callback_data=f"hrp:{sid}:{page + 1}"))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_hdrezka_quality_keyboard(sid: str, tid: int, qualities: list, is_premium: bool = False) -> InlineKeyboardMarkup:
    """Качества кнопками. HDRezka-премиум (Ultra/2K/4K) скрыты; >720p — наш Premium-замок."""
    rows = []
    for i, q in enumerate(qualities):
        ql = q.lower()
        if any(p in ql for p in _HDREZKA_PREMIUM_Q):
            continue  # недоступно без подписки HDRezka — не показываем
        m = re.search(r"(\d+)", q)
        height = int(m.group(1)) if m else 0
        if height > FREE_QUALITY_LIMIT and not is_premium:
            rows.append([InlineKeyboardButton(text=f"🔒 {q}", callback_data="buy_premium")])
        else:
            rows.append([InlineKeyboardButton(text=q, callback_data=f"hrq:{sid}:{tid}:{i}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
