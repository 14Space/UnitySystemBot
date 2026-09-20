"""На каком языке бот отвечает на вопрос к ИИ.

Баг, из-за которого это появилось: язык брался из настроек Telegram, а не из вопроса.
У человека английский интерфейс — и на «как умер александр македонский?» приходил
ответ на английском.
"""
import pytest

from bot.features.ai.chat import _reply_lang
from bot.utils.i18n import lang_of_text


@pytest.mark.parametrize("text, expected", [
    ("как умер александр македонский?", "ru"),
    ("что делаешь", "ru"),
    ("скажи как дела", "ru"),
    ("будь добр, скажи время", "ru"),
    ("як помер олександр македонський", "uk"),
    ("привіт, що робиш", "uk"),
    ("дуже дякую", "uk"),
    ("how did alexander the great die", "en"),
    ("what are you doing", "en"),
])
def test_language_is_taken_from_the_text(text, expected):
    assert lang_of_text(text, "xx") == expected


@pytest.mark.parametrize("text", ["ок", "?", "123", "", "   "])
def test_too_short_falls_back_to_interface_language(text):
    """По «ок» язык не определить — держимся настроек Telegram."""
    assert lang_of_text(text, "en") == "en"


def test_answer_follows_the_last_question():
    """В двуязычной ветке отвечаем на языке последней реплики, а не первой."""
    history = [
        {"role": "user", "content": "how did alexander die"},
        {"role": "assistant", "content": "He fell ill in Babylon"},
        {"role": "user", "content": "а от чего именно он заболел"},
    ]
    assert _reply_lang(history, "en") == "ru"


def test_interface_language_is_used_when_question_is_unclear():
    history = [{"role": "user", "content": "ок"}]
    assert _reply_lang(history, "uk") == "uk"


def test_empty_history_keeps_interface_language():
    assert _reply_lang([], "en") == "en"
