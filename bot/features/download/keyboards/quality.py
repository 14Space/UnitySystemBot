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
from bot.config import FREE_QUALITY_LIMIT


def build_quality_keyboard(url_id: str, available: list[int], is_premium: bool = False) -> InlineKeyboardMarkup:
    """
    Premium — все доступные качества кнопками.
    Бесплатно — лучшее качество до 720p, выше = замок (предложение купить Premium).
    """
    builder = InlineKeyboardBuilder()
    if not available:
        return builder.as_markup()

    if is_premium:
        for q in sorted(available):
            builder.button(
                text=QUALITY_LABELS.get(q, f"{q}p"),
                callback_data=f"quality:{q}:{url_id}"
            )
        builder.adjust(4)
        return builder.as_markup()

    # Бесплатное качество: лучшее из доступного, но не выше 720p.
    free_candidates = [q for q in available if q <= FREE_QUALITY_LIMIT]
    free_q = max(free_candidates) if free_candidates else min(available)

    builder.button(
        text=QUALITY_LABELS.get(free_q, f"{free_q}p"),
        callback_data=f"quality:{free_q}:{url_id}"
    )

    # Замки: реально существующие качества выше бесплатного → предложение купить Premium
    for q in ALL_QUALITIES:
        if q > free_q and q in available:
            builder.button(
                text=f"🔒 {QUALITY_LABELS.get(q, f'{q}p')}",
                callback_data="buy_premium"
            )

    builder.adjust(4)
    return builder.as_markup()
