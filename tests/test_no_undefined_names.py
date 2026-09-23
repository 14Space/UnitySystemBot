"""Ни одного обращения к имени, которого нет.

Повод — падение 23.09.2026. Я добавил в main.py пул потоков и забыл импорт
`concurrent.futures`: файл разбирается, тесты зелёные, бот падает при старте на
сервере. То же самое нашлось ещё в четырёх местах — все от правок, сделанных
скриптом: пропавший импорт `net`, пропавший `logger`, пропавший `GROUP_TYPES` и
константа премиум-качеств HDRezka, которую снесло вместе с уборкой мёртвого кода.

Обычные тесты этого не ловят: NameError случается только когда до строки доходит
выполнение — в main.py это старт бота, в клавиатуре качеств HDRezka это показ кнопок.
Поэтому проверяем весь код разбором, как это делает pyflakes.

Ругань про «импортировано и не используется» нас не интересует: это стиль, а не
поломка. Ловим ровно то, что падает: обращение к имени, которого нет.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Что считаем поломкой. Формулировки pyflakes стабильны годами.
_BREAKING = ("undefined name", "undefined local", "syntax error")


def test_no_undefined_names():
    result = subprocess.run(
        [sys.executable, "-m", "pyflakes", str(ROOT / "bot"), str(ROOT / "tools")],
        capture_output=True, text=True, timeout=180,
    )
    if "No module named pyflakes" in (result.stderr or ""):
        import pytest
        pytest.skip("pyflakes не установлен — проверка пропущена")

    broken = [line for line in (result.stdout or "").splitlines()
              if any(mark in line.lower() for mark in _BREAKING)]
    assert not broken, (
        "код обращается к именам, которых нет (бот упадёт при выполнении):\n"
        + "\n".join(broken))
