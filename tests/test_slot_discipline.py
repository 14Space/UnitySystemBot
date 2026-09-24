"""Правило, которое проверяется по КОДУ, а не по поведению.

23.09.2026 бот перестал отвечать на ссылки: слот очереди заняли, а освобождение
стояло в `finally` ниже — за отправкой сообщения, которая не прошла. Слот остался
занятым навсегда, и все загрузки встали в очередь за мёртвым держателем. Поведенческим
тестом такое не поймать: нужен сбой ровно в одной строке между двумя другими.

Поэтому проверяем форму: после `limits.acquire(...)` следующим содержательным
оператором обязан быть `try`, в `finally` которого есть `limits.release(...)`.
Между ними можно только объявить переменные под будущую уборку (`path = None`).

Тот же класс ошибок в любом новом месте сломает этот тест, а не бота.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent / "bot"


# Что считаем «занятием»: слот очереди и отметка «этот человек уже качает». Вторая
# не останавливает бота целиком, но одного человека запирает так же насмерть.
_TAKE = {"acquire": ("release",), "acquire_or_tell": ("release",),
         "add": ("discard", "remove")}


def _taken(node) -> tuple[str, tuple[str, ...]] | None:
    """Если оператор занимает ресурс — чем его положено отпускать.

    Форм две: голое `await limits.acquire(...)` и `slot = await limits.acquire(...)`
    (слот освобождается по номеру, см. limits). Вторую тоже обязаны видеть: иначе
    правило тихо перестанет что-либо проверять после безобидной правки.
    """
    inner = None
    if isinstance(node, ast.Expr):
        inner = node.value
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
        inner = node.value
    if isinstance(inner, ast.Await):
        inner = inner.value
    if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)):
        return None
    attr = inner.func.attr
    if attr not in _TAKE:
        return None
    if attr == "add":
        # Только наша отметка занятости, а не любое множество в коде.
        owner = inner.func.value
        if not (isinstance(owner, ast.Name) and owner.id == "ACTIVE_DOWNLOADS"):
            return None
    return attr, _TAKE[attr]


def _is_acquire(node) -> bool:
    return _taken(node) is not None


def _mentions(node, names: tuple[str, ...]) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr in names
               for n in ast.walk(node))


def _is_plain_declaration(node) -> bool:
    """Объявление переменной без вызовов: `path = None`, `files = mp4 = None`."""
    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
        return False
    value = node.value
    return value is None or isinstance(value, (ast.Constant, ast.Name, ast.List, ast.Dict))


def _violations_in(source: str, name: str) -> list[str]:
    tree = ast.parse(source)
    bad = []
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if not isinstance(body, list):
            continue
        for i, node in enumerate(body):
            taken = _taken(node)
            if taken is None:
                continue
            attr, frees = taken
            where = f"{name}:{node.lineno}"
            # Занятие может стоять и ПЕРВЫМ в защищённом блоке — это тоже правильно.
            in_try = isinstance(parent, ast.Try) and body is parent.body
            if in_try and _mentions(
                    ast.Module(body=parent.finalbody, type_ignores=[]), frees):
                continue
            rest = [n for n in body[i + 1:] if not _is_plain_declaration(n)]
            # Между занятием слота и try допускаем занятие второго ресурса: оба
            # освобождаются в одном и том же finally.
            rest = [n for n in rest if _taken(n) is None]
            if not rest or not isinstance(rest[0], ast.Try):
                bad.append(f"{where}: после «{attr}» нет try")
                continue
            if not _mentions(ast.Module(body=rest[0].finalbody, type_ignores=[]), frees):
                bad.append(f"{where}: в finally нет «{'/'.join(frees)}»")
    return bad


def _violations(path: pathlib.Path) -> list[str]:
    return _violations_in(path.read_text(encoding="utf-8"), path.name)


# Сам ограничитель не проверяем: внутри него acquire — это реализация, а не занятие
# слота под работу (см. limits.acquire_or_tell, который занимает слот для вызывающего).
FILES = sorted(p for p in ROOT.rglob("*.py") if p.name != "limits.py")


def test_there_are_places_to_check():
    """Страховка от «тест зелёный, потому что ничего не нашёл».

    Мест немного, и это нарочно: загрузки занимают слот через скелет job.py (слот
    очереди и отметка «уже качает»), а не каждая по-своему. Плюс расшифровка."""
    found = sum(1 for f in FILES
                for n in ast.walk(ast.parse(f.read_text(encoding="utf-8")))
                if _is_acquire(n))
    assert found >= 3


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_slot_is_released_in_finally(path):
    problems = _violations(path)
    assert not problems, "занятый слот может не освободиться:\n" + "\n".join(problems)


# --- Проверка самой проверки: правило должно ловить ту самую ошибку ---------
# Без этого тест мог бы тихо перестать что-либо находить, и мы бы об этом не узнали.

_BAD = """
async def download(message):
    slot = await limits.acquire(limits.HEAVY)
    status = await message.reply("качаю")   # упадёт — слот останется занятым
    try:
        await work()
    finally:
        await limits.release(limits.HEAVY)
"""

_NO_RELEASE = """
async def download(message):
    await limits.acquire(limits.HEAVY)
    try:
        await work()
    finally:
        cleanup()
"""

_GOOD = """
async def download(message):
    await limits.acquire(limits.HEAVY)
    status = None
    path = None
    try:
        status = await message.reply("качаю")
        await work()
    finally:
        cleanup(path)
        await limits.release(limits.HEAVY)
"""


def test_rule_catches_a_send_before_try():
    """Ровно та форма, которая 23.09.2026 остановила бота."""
    assert _violations_in(_BAD, "образец")


def test_rule_catches_a_missing_release():
    assert _violations_in(_NO_RELEASE, "образец")


def test_rule_accepts_the_correct_form():
    assert _violations_in(_GOOD, "образец") == []


_BAD_MARKER = """
async def download_all(bot, user_id):
    ACTIVE_DOWNLOADS.add(user_id)
    status = await bot.send_message(1, "качаю")   # упадёт — человек заперт навсегда
    try:
        await work()
    finally:
        ACTIVE_DOWNLOADS.discard(user_id)
"""

_GOOD_PAIR = """
async def download(bot, user_id):
    ACTIVE_DOWNLOADS.add(user_id)
    await limits.acquire(limits.HEAVY)
    status = None
    try:
        status = await bot.send_message(1, "качаю")
    finally:
        await limits.release(limits.HEAVY)
        ACTIVE_DOWNLOADS.discard(user_id)
"""


def test_rule_catches_a_stuck_busy_marker():
    """Отметка «уже качает» запирает не бота, а человека — но так же насмерть."""
    assert _violations_in(_BAD_MARKER, "образец")


def test_rule_accepts_two_resources_in_one_finally():
    assert _violations_in(_GOOD_PAIR, "образец") == []


_BAD_TOKEN_FORM = """
async def download(message):
    slot = await limits.acquire(limits.LIGHT)
    status = await message.reply("качаю")   # упадёт — слот останется занятым
    try:
        await work()
    finally:
        await limits.release(limits.LIGHT, slot)
"""


def test_rule_sees_the_token_form():
    """Слот, занятый «с номером», проверяется так же строго."""
    assert _violations_in(_BAD_TOKEN_FORM, "образец")


def test_second_heavy_download_is_refused_even_when_clicks_race():
    """Повторный аудит: проверка «уже качает» и отметка стояли разными шагами, а между
    ними – ожидания (ответ на нажатие, удаление меню). Два быстрых нажатия на разные
    качества проходили проверку оба. Теперь это один шаг внутри exclusive()."""
    import asyncio
    from bot.features.download import job

    seen = []

    async def click():
        async with job.exclusive(777) as mine:
            seen.append(mine)
            if mine:
                await asyncio.sleep(0.01)      # «загрузка» идёт, второе нажатие приходит

    async def run():
        await asyncio.gather(click(), click())

    asyncio.run(run())
    assert sorted(seen) == [False, True]
    assert 777 not in job.ACTIVE_DOWNLOADS     # отметка снята после загрузки
