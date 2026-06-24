from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

PAGE_SIZE = 10


def build_tracklist_keyboard(coll_id: str, tracks: list, page: int, is_premium: bool = False) -> InlineKeyboardMarkup:
    """
    Клавиатура списка треков альбома/плейлиста: по 10 на страницу.
    «Скачать всё» — только для Premium (иначе замок → предложение купить).
    """
    total_pages = (len(tracks) + PAGE_SIZE - 1) // PAGE_SIZE
    start = page * PAGE_SIZE
    chunk = tracks[start:start + PAGE_SIZE]

    rows = []
    if is_premium:
        rows.append([InlineKeyboardButton(text="⬇️ Скачать всё", callback_data=f"dlall:{coll_id}")])
    else:
        rows.append([InlineKeyboardButton(text="🔒 Скачать всё", callback_data="buy_premium")])

    for i, track in enumerate(chunk, start=start):
        label = f"{i + 1}. {track['title']}"
        if len(label) > 40:
            label = label[:39] + "…"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"sptrk:{coll_id}:{i}")])

    # Навигация по страницам
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"sppage:{coll_id}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд →", callback_data=f"sppage:{coll_id}:{page + 1}"))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)
