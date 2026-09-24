"""Скачанный файл должен исчезать с диска в любом случае.

До этого уборка стояла ТОЛЬКО на успешном пути: упала отправка (файл больше 2 ГБ,
оборвалась связь, у бота нет прав в чате) — и скачанное оставалось на диске до
перезапуска бота. На фильмах это гигабайты за раз.

Теперь скелет загрузки один (bot/features/download/job.py), и проверяем мы его: и по
форме кода, и по поведению.
"""
import ast
import asyncio
import pathlib

from bot.features.download import job
from bot.utils import files

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _func(rel: str, name: str):
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} не найдена — тест устарел")


def test_cleanup_always_lives_in_finally():
    """Уборка обязана быть в finally скелета: на успешном пути её недостаточно."""
    node = _func("bot/features/download/job.py", "produce")
    finals = [ast.unparse(ast.Module(body=n.finalbody, type_ignores=[]))
              for n in ast.walk(node) if isinstance(n, ast.Try)]
    assert any("files.remove" in f for f in finals), "уборка не в finally"


def test_link_does_not_clean_up_by_hand():
    """Загрузки в link.py идут через скелет: своя уборка там – верный признак того,
    что скелет снова начали переписывать руками."""
    src = "\n".join(p.read_text(encoding="utf-8") for p in
                    [ROOT / "bot/features/download/link.py",
                     *(ROOT / "bot/features/download/flows").glob("*.py")])
    for name in ("_cleanup", "limits.acquire", "limits.release", "ACTIVE_DOWNLOADS"):
        assert name not in src, f"{name} снова в link.py – загрузка мимо job.py"


def test_files_are_removed_when_sending_fails(tmp_path):
    """Упала отправка – скачанное всё равно исчезает, а человеку сказали о сбое."""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"1")
    told = []

    async def work(paths):
        paths.keep(str(video))
        raise RuntimeError("Telegram не принял файл")

    async def on_error(e):
        told.append(str(e))

    asyncio.run(job.produce(cache=None, work=work, on_error=on_error))
    assert not video.exists()
    assert told == ["Telegram не принял файл"]


def test_remove_handles_everything(tmp_path):
    """Помощник принимает и None, и путь, и список — в finally переменная бывает любой."""
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.jpg"
    a.write_bytes(b"1")
    b.write_bytes(b"2")

    files.remove(None)                            # ещё не скачали — не падаем
    files.remove(str(a), [str(b)])
    assert not a.exists() and not b.exists()


def test_remove_survives_missing_file(tmp_path):
    files.remove(str(tmp_path / "нет-такого.mp4"))
