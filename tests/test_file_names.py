"""Имя файла, которое видит человек.

Имя собирается из названия ролика, то есть из чужого текста: там бывают эмодзи,
косые черты, переводы строк и километровые заголовки. Плохое имя — это не «некрасиво»,
а отказ файловой системы записать файл.
"""
from bot.utils.tg_files import safe_name, display_name


def test_removes_forbidden_characters():
    assert "/" not in safe_name("AC/DC - Thunderstruck")
    assert safe_name('a:b*c?d"e<f>g|h') == "abcdefgh"


def test_strips_newlines_and_extra_spaces():
    assert safe_name("строка\nвторая   третья") == "строка вторая третья"


def test_no_trailing_dots_or_spaces():
    assert safe_name("название...  ") == "название"


def test_length_is_capped_in_bytes():
    # Кириллица занимает по два байта: ограничение считается в байтах, иначе
    # русское название пролезает мимо потолка и файловая система его отвергает.
    assert len(safe_name("я" * 300).encode("utf-8")) <= 120


def test_display_name_full():
    assert display_name("Клип", "1080p", "mp4") == "Клип [1080p] @UnitySystemBot.mp4"


def test_display_name_accepts_bare_number():
    assert display_name("Клип", "720", "mp4") == "Клип [720p] @UnitySystemBot.mp4"


def test_display_name_without_quality_has_no_empty_brackets():
    assert display_name("Фото", "", "jpg") == "Фото @UnitySystemBot.jpg"


def test_display_name_survives_empty_title():
    # Название иногда не приходит вовсе — имя всё равно должно получиться.
    assert display_name("", "", "mp4") == "UnitySystemBot @UnitySystemBot.mp4"
