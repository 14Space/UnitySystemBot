from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

PAGE_SIZE = 10


def build_tracklist_keyboard(coll_id: str, tracks: list, page: int) -> InlineKeyboardMarkup:
    """
    Клавиатура списка треков альбома/плейлиста: по 10 на страницу.
    coll_id — короткий id коллекции в памяти (SPOTIFY_STORE).
    """
    total_pages = (len(tracks) + PAGE_SIZE - 1) // PAGE_SIZE
    start = page * PAGE_SIZE
    chunk = tracks[start:start + PAGE_SIZE]

    rows = []
    # «Скачать всё» — закрытая опция (задел под монетизацию)
    rows.append([InlineKeyboardButton(text="🔒 Скачать всё", callback_data="stub:download_all")])

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
