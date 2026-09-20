"""Две разные беды с куками — два разных приёма.

Instagram присылает УРЕЗАННЫЙ набор и затирает ключ входа → ему даём одноразовую
копию, оригинал не трогаем. Google, наоборот, РОТИРУЕТ токены: старый через несколько
часов перестаёт приниматься → ему нужна постоянная рабочая копия, где обновления
сохраняются между запросами.
"""
import os
import time

from bot.utils import cookie_files


def test_disposable_does_not_touch_the_original(tmp_path):
    src = tmp_path / "instagram_cookies.txt"
    src.write_text("оригинал")
    copy = cookie_files.disposable(str(src), str(tmp_path / "tmp"))
    assert copy and copy != str(src)
    with open(copy, "w") as f:                 # «yt-dlp» испортил копию
        f.write("урезанный набор")
    assert src.read_text() == "оригинал"


def test_disposable_returns_none_without_file(tmp_path):
    assert cookie_files.disposable(str(tmp_path / "нет.txt"), str(tmp_path)) is None


def test_working_copy_keeps_updates(tmp_path):
    src = tmp_path / "youtube_cookies.txt"
    src.write_text("первый токен")
    work = cookie_files.working(str(src), str(tmp_path / "w"))
    assert work

    with open(work, "w") as f:                 # Google прислал свежий токен
        f.write("обновлённый токен")

    again = cookie_files.working(str(src), str(tmp_path / "w"))
    assert again == work
    assert open(work).read() == "обновлённый токен"   # обновление не потерялось
    assert src.read_text() == "первый токен"          # оригинал нетронут


def test_working_copy_restarts_from_a_fresh_original(tmp_path):
    """Человек перевыгрузил куки — рабочая копия должна начаться заново с них."""
    src = tmp_path / "youtube_cookies.txt"
    src.write_text("старое")
    work = cookie_files.working(str(src), str(tmp_path / "w"))
    with open(work, "w") as f:
        f.write("наработанное")

    time.sleep(0.01)
    src.write_text("свежая выгрузка")
    os.utime(src, None)

    cookie_files.working(str(src), str(tmp_path / "w"))
    assert open(work).read() == "свежая выгрузка"


def test_working_returns_none_without_file(tmp_path):
    assert cookie_files.working(str(tmp_path / "нет.txt"), str(tmp_path)) is None
