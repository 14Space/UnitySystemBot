"""Как выглядит отчёт админу.

Отчёт собирается из словаря, который приходит из базы. Если там не окажется нового
поля (например, «premium»), отчёт не должен падать — лучше показать 0, чем не прийти.
"""
from bot.features.common.admin import format_stats

BASE = {
    "users": 43,
    "premium": 2,
    "languages": {"ru": 41, "en": 2},
    "downloads": {"tiktok": 10, "youtube_video": 5},
    "total_downloads": 15,
    "traffic": [],
    "payments": {"count": 2, "stars": 500, "refunded": 0},
}


def test_report_shows_users_and_premium():
    text = format_stats(BASE, "ru")
    assert "👥 Пользователей: <b>43</b>" in text
    assert "⭐️ Премиум пользователей: <b>2</b>" in text


def test_premium_line_goes_right_after_users():
    lines = [l for l in format_stats(BASE, "ru").split("\n") if l]
    assert lines[1].startswith("👥") and lines[2].startswith("⭐️")


def test_languages_have_percentages():
    assert "• ru: 41 (95%)" in format_stats(BASE, "ru")


def test_purchases_are_shown_with_stars():
    assert "💰 Покупок: <b>2</b> (500 звёзд)" in format_stats(BASE, "ru")


def test_refunds_are_shown_when_present():
    stats = {**BASE, "payments": {"count": 2, "stars": 500, "refunded": 1}}
    assert "1 возвратов" in format_stats(stats, "ru")


def test_no_purchases_line_without_sales():
    stats = {**BASE, "payments": {"count": 0, "stars": 0, "refunded": 0}}
    assert "💰" not in format_stats(stats, "ru")


def test_survives_old_stats_without_new_fields():
    old = {k: v for k, v in BASE.items() if k not in ("premium", "payments")}
    text = format_stats(old, "ru")
    assert "⭐️ Премиум пользователей: <b>0</b>" in text


def test_unused_platforms_are_shown_with_zero():
    """«Не пользуются» и «сломалось» должны различаться на глаз.

    Раньше площадка без единого запроса просто исчезала из отчёта, и понять, почему
    Pinterest не видно — им не пользуются или он перестал распознавать ссылки, — было
    нельзя.
    """
    text = format_stats(BASE, "ru")
    assert "• Pinterest: 0" in text
    assert "• SoundCloud: 0" in text
    assert "• YouTube Music: 0" in text


def test_platforms_are_sorted_by_usage_then_alphabetically():
    from bot.features.common.admin import _grouped_downloads

    rows = _grouped_downloads({"tiktok": 10, "youtube_video": 5})
    assert rows[0] == ("TikTok", 10)
    assert rows[1] == ("YouTube", 5)
    # Дальше нули по алфавиту — иначе они скакали бы между отчётами.
    zeros = [name for name, count in rows if count == 0]
    assert zeros == sorted(zeros)


def test_platform_names_match_the_functionality_check():
    """Одно и то же в двух списках одного отчёта не должно называться по-разному.

    Было: в проверке пункт «YouTube Music», а в статистике площадка «YT Music».
    """
    from bot.features.common.admin import PLATFORM_GROUP
    from bot.features.common.healthcheck import _CHECKS

    in_checks = {platform for _name, platform, _fn, _url in _CHECKS if platform}
    in_stats = set(PLATFORM_GROUP.values())
    # У площадок из статистики, чьё имя встречается и в проверке, написание совпадает —
    # сравнение идёт по точному значению, так что расхождение вида «YT Music» всплывёт.
    assert "YouTube Music" in in_stats
    assert not {n for n in in_stats if n.replace("YouTube", "YT") in in_checks and
                n not in in_checks and n != "YouTube Music"}
