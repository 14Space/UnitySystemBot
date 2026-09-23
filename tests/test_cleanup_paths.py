"""Скачанный файл должен исчезать с диска в любом случае.

До этого уборка стояла ТОЛЬКО на успешном пути: упала отправка (файл больше 2 ГБ,
оборвалась связь, у бота нет прав в чате) — и скачанное оставалось на диске до
перезапуска бота. На фильмах это гигабайты за раз.
"""
import ast
import inspect
import pathlib

from bot.features.download import link

SOURCE = pathlib.Path(link.__file__).read_text(encoding="utf-8")


def _download_blocks():
    """Блоки try, внутри которых что-то скачивается и упоминается уборка.

    Разбором кода, а не поиском по тексту: прежняя версия искала регулярным
    выражением «try … except … finally» и спотыкалась о любой соседний try без
    except — правило она при этом проверяла случайно, а не по существу.
    """
    tree = ast.parse(SOURCE)
    blocks = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        joiner = chr(10)
        body = joiner.join(ast.unparse(n) for n in node.body)
        finallybody = joiner.join(ast.unparse(n) for n in node.finalbody)
        handlers = joiner.join(ast.unparse(n) for n in node.handlers)
        if "to_thread" in body and "_cleanup" in body + finallybody + handlers:
            blocks.append((node.lineno, body, handlers, finallybody))
    return blocks


def test_there_are_blocks_to_check():
    """Страховка от «зелено, потому что ничего не нашли»."""
    assert len(_download_blocks()) >= 4


def test_cleanup_always_lives_in_finally():
    """Уборка обязана быть в finally: на успешном пути её недостаточно — упала
    отправка, и скачанное осталось на диске до перезапуска бота."""
    leaking = [f"строка {line}" for line, body, handlers, final in _download_blocks()
               if "_cleanup" not in final]
    assert not leaking, f"уборка не в finally: {leaking}"


def test_cleanup_all_handles_everything(tmp_path):
    """Помощник принимает и None, и путь, и список — в finally переменная бывает любой."""
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.jpg"
    a.write_bytes(b"1")
    b.write_bytes(b"2")

    link._cleanup_all(None)                       # ещё не скачали — не падаем
    link._cleanup_all(str(a), [str(b)])
    assert not a.exists() and not b.exists()


def test_cleanup_all_survives_missing_file(tmp_path):
    link._cleanup_all(str(tmp_path / "нет-такого.mp4"))
