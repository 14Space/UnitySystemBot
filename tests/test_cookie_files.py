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


def _netscape(*cookies) -> str:
    """Файл кук в формате Netscape: домен, флаг, путь, secure, срок, имя, значение."""
    head = "# Netscape HTTP Cookie File\n"
    return head + "".join(
        f".youtube.com\tTRUE\t/\tTRUE\t2000000000\t{name}\t{value}\n"
        for name, value in cookies)


def test_lost_cookies_come_back_from_the_original(tmp_path):
    """21.09.2026: какой-то запрос вернулся «разлогиненным», yt-dlp записал это
    поверх рабочей копии — и ключ входа пропал при живом аккаунте."""
    src = tmp_path / "youtube_cookies.txt"
    src.write_text(_netscape(("SID", "ключ"), ("HSID", "ключ2"), ("SIDCC", "старый")),
                   encoding="utf-8")
    work = cookie_files.working(str(src), str(tmp_path / "w"))

    # «yt-dlp» сохранил набор гостя: ключа входа нет, зато SIDCC обновился.
    with open(work, "w", encoding="utf-8") as f:
        f.write(_netscape(("SIDCC", "свежий"), ("SOCS", "согласие")))

    cookie_files.working(str(src), str(tmp_path / "w"))
    rows = cookie_files._rows(work)
    names = {name: line.split("\t")[6] for (_, _, name), line in rows.items()}
    assert names["SID"] == "ключ" and names["HSID"] == "ключ2"   # вернули
    assert names["SIDCC"] == "свежий"                            # ротацию не тронули
    assert names["SOCS"] == "согласие"                           # чужого не выкинули


def test_nothing_is_rewritten_when_all_cookies_are_in_place(tmp_path):
    src = tmp_path / "youtube_cookies.txt"
    src.write_text(_netscape(("SID", "ключ")), encoding="utf-8")
    work = cookie_files.working(str(src), str(tmp_path / "w"))
    with open(work, "w", encoding="utf-8") as f:
        f.write(_netscape(("SID", "обновлённый ключ")))
    before = os.path.getmtime(work)

    time.sleep(0.01)
    cookie_files.working(str(src), str(tmp_path / "w"))
    assert os.path.getmtime(work) == before
    assert "обновлённый ключ" in open(work, encoding="utf-8").read()


def test_work_copy_lives_next_to_the_original(tmp_path):
    """Не в папке загрузок: её вычищают при каждом старте бота."""
    from bot.features.download.downloaders import ytdlp_wrapper as y

    src = tmp_path / "youtube_cookies.txt"
    src.write_text(_netscape(("SID", "ключ")), encoding="utf-8")
    old = y.YOUTUBE_COOKIES
    y.YOUTUBE_COOKIES = str(src)
    try:
        opts = y._cookie_opts("https://www.youtube.com/watch?v=x")
    finally:
        y.YOUTUBE_COOKIES = old
    assert os.path.dirname(opts["cookiefile"]) == str(tmp_path)
