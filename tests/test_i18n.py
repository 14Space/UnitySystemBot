"""Надписи бота на трёх языках.

Недостающий перевод не падает с ошибкой: t() тихо подставит русский текст, а то и
сам ключ («err_private»). Поэтому дыру в переводах ловим тестом, а не глазами.
"""
import pytest

from bot.utils.i18n import TEXTS, SUPPORTED, t, lang_of


class _User:
    def __init__(self, code):
        self.language_code = code


@pytest.mark.parametrize("lang", sorted(SUPPORTED))
def test_every_text_exists_in_every_language(lang):
    missing = [key for key, entry in TEXTS.items() if lang not in entry]
    assert not missing, f"нет перевода на {lang}: {missing}"


def test_no_text_is_empty():
    empty = [(k, l) for k, e in TEXTS.items() for l, v in e.items() if not (v or "").strip()]
    assert not empty


def test_placeholders_match_across_languages():
    """Подстановки вида {count} должны быть во всех языках одинаковые: лишняя в одном
    языке роняет t() с KeyError ровно у тех пользователей, у кого этот язык."""
    import re
    bad = []
    for key, entry in TEXTS.items():
        sets = {lang: set(re.findall(r"{(\w+)}", text)) for lang, text in entry.items()}
        if len(set(map(frozenset, sets.values()))) > 1:
            bad.append((key, sets))
    assert not bad, bad


def test_lang_of_falls_back_to_russian_without_code():
    assert lang_of(_User(None)) == "ru"


def test_lang_of_uses_english_for_unsupported():
    assert lang_of(_User("de")) == "en"


def test_t_substitutes_values():
    assert "{" not in t("rep_users", "ru")
