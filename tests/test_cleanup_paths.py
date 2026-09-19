"""Скачанный файл должен исчезать с диска в любом случае.

До этого уборка стояла ТОЛЬКО на успешном пути: упала отправка (файл больше 2 ГБ,
оборвалась связь, у бота нет прав в чате) — и скачанное оставалось на диске до
перезапуска бота. На фильмах это гигабайты за раз.
"""
import inspect
import re

from bot.features.download import link


def _functions_that_download():
    """Куски кода, где что-то скачивается: они обязаны прибирать за собой в finally."""
    source = inspect.getsource(link)
    blocks = re.findall(
        r"\n(\s+)try:\n(.*?)\n\1except[^\n]*\n(.*?)(?=\n\s+finally:|\n\s*async def|\n\s*def )",
        source, re.S)
    return [(body, handler) for _indent, body, handler in blocks
            if "asyncio.to_thread" in body and "_cleanup" in body + handler]


def test_no_cleanup_only_on_success():
    """Ни в одном блоке уборка не должна жить только в успешной ветке."""
    leaking = [body.strip().splitlines()[0]
               for body, handler in _functions_that_download()
               if "_cleanup" in body and "_cleanup" not in handler]
    assert not leaking, f"уборка только при успехе: {leaking}"


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
