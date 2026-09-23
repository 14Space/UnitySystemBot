"""Одно нажатие — один результат, и видимый признак работы в шапке чата.

Повод: 22.09.2026 человек в личке нажал «Фото» у слайдшоу TikTok пять раз и получил
пять одинаковых наборов. Кнопки убираются только ПОСЛЕ отправки, а за время скачивания
успевают запуститься несколько обработчиков. У тяжёлых загрузок защита была своя
(«дождись текущей»), у лёгких — никакой.
"""
import ast
import asyncio
import pathlib

import pytest

from bot.features.download import link

SOURCE = pathlib.Path(link.__file__).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean():
    link._IN_PROGRESS.clear()
    yield
    link._IN_PROGRESS.clear()


def test_second_identical_click_is_refused():
    assert link._claim(1, "sid", "photos") is True
    assert link._claim(1, "sid", "photos") is False      # то же нажатие
    assert link._claim(1, "sid", "video") is True        # другой формат — можно
    assert link._claim(2, "sid", "photos") is True       # другой чат — можно


def test_click_is_free_again_after_release():
    assert link._claim(1, "sid", "photos") is True
    link._unclaim(1, "sid", "photos")
    assert link._claim(1, "sid", "photos") is True


def _handler(name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"обработчик {name} не найден — тест устарел")


@pytest.mark.parametrize("handler", ["handle_tiktok_slideshow", "handle_collection_track"])
def test_guarded_handlers_always_release(handler):
    """Занял нажатие — обязан отпустить, иначе кнопка замолчит навсегда."""
    node = _handler(handler)
    body = ast.unparse(node)
    assert "_claim(" in body, f"{handler}: защиты от повторного нажатия нет"
    releases = [n for n in ast.walk(node)
                if isinstance(n, ast.Try)
                and "_unclaim" in ast.unparse(ast.Module(body=n.finalbody, type_ignores=[]))]
    assert releases, f"{handler}: освобождение не в finally — кнопка может залипнуть"


def test_chat_action_survives_a_broken_bot():
    """Действие в шапке чата — мелочь, из-за которой не должна падать загрузка."""
    from bot.utils import chat_action

    class BrokenBot:
        id = 1                      # у настоящего бота он есть — библиотека его читает

        async def send_chat_action(self, **kwargs):
            raise RuntimeError("нет прав писать в этот чат")

    async def scenario():
        await chat_action.once(BrokenBot(), 1)          # не должно бросить
        async with chat_action.show(BrokenBot(), 1, chat_action.VIDEO):
            return "работа продолжилась"

    assert asyncio.run(scenario()) == "работа продолжилась"


def test_actions_are_shown_where_it_takes_time():
    """Скачивание идёт секунды и минуты — человек должен видеть, что бот занят."""
    for name in ("_handle_simple_video", "_handle_files", "_do_download_audio"):
        assert "chat_action" in ast.unparse(_handler(name)), \
            f"{name}: нет признака работы в шапке чата"
