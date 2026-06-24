from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


QUALITY_LABELS = {
    144: "144p",
    240: "240p",
    360: "360p",
    480: "480p",
    720: "720p HD",
    1080: "1080p FHD",
    1440: "1440p 2K",
    2160: "2160p 4K",
}


ALL_QUALITIES = [144, 240, 360, 480, 720, 1080, 1440, 2160]

# Граница бесплатного качества — 720p. Всё выше = премиум (заглушка).
FREE_LIMIT = 720


def build_quality_keyboard(url_id: str, available: list[int]) -> InlineKeyboardMarkup:
    """
    Бесплатная кнопка — 720p (или максимум видео, если оно ниже 720p).
    Качества выше 720p — с замком (премиум, в разработке).
    """
    builder = InlineKeyboardBuilder()
    if not available:
        return builder.as_markup()

    # Бесплатное качество: лучшее из доступного, но не выше 720p.
    free_candidates = [q for q in available if q <= FREE_LIMIT]
    if free_candidates:
        free_q = max(free_candidates)
    else:
        # У видео нет качеств 720p и ниже — отдаём самое низкое доступное.
        free_q = min(available)

    # Бесплатная кнопка
    builder.button(
        text=QUALITY_LABELS.get(free_q, f"{free_q}p"),
        callback_data=f"quality:{free_q}:{url_id}"
    )

    # Премиум-кнопки: только реально существующие у видео качества выше бесплатного
    for q in ALL_QUALITIES:
        if q > free_q and q in available:
            builder.button(
                text=f"🔒 {QUALITY_LABELS.get(q, f'{q}p')}",
                callback_data="stub:premium_quality"
            )

    builder.adjust(4)
    return builder.as_markup()
