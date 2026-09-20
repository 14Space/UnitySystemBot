"""Перенос кук в профиль браузера (tools/cookie_sync.py).

Главный способ «войти» в рабочий профиль — не логин руками (Instagram показывает в
автоматизированном окне капчу, которая там часто вообще не рисуется), а перенос уже
выгруженных кук: в обычном браузере человек вошёл как человек, все проверки пройдены.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import cookie_sync as cs


def _write(tmp_path, body):
    p = tmp_path / "cookies.txt"
    p.write_text("# Netscape HTTP Cookie File\n" + body, encoding="utf-8")
    return str(p)


def test_reads_exported_file(tmp_path):
    path = _write(tmp_path,
                  ".instagram.com\tTRUE\t/\tTRUE\t1790000000\tsessionid\tabc123\n"
                  ".instagram.com\tTRUE\t/\tFALSE\t0\tcsrftoken\txyz\n")
    cookies = cs._read_netscape(path)
    assert {c["name"] for c in cookies} == {"sessionid", "csrftoken"}
    by_name = {c["name"]: c for c in cookies}
    assert by_name["sessionid"]["value"] == "abc123"
    assert by_name["sessionid"]["secure"] is True
    assert by_name["sessionid"]["expires"] == 1790000000


def test_session_cookie_gets_a_real_expiry(tmp_path):
    """Куке без срока проставляется годовой.

    Иначе браузер считает её сессионной и не пишет на диск: при переносе так пропадали
    YSC, GPS и __Secure-1PSIDRTS, а без них YouTube отвечал «Sign in to confirm your age».
    """
    import time

    path = _write(tmp_path, ".x.com\tTRUE\t/\tTRUE\t0\tauth_token\tzzz\n")
    expires = cs._read_netscape(path)[0]["expires"]
    assert expires > time.time() + 300 * 24 * 3600


def test_roundtrip_through_our_own_writer(tmp_path):
    """Выгруженное нами читается нами же: формат не разъедется между версиями."""
    cookies = [{"domain": ".youtube.com", "path": "/", "secure": True,
                "expires": 1790000000, "name": "SID", "value": "v"}]
    path = tmp_path / "out.txt"
    path.write_text(cs._netscape(cookies), encoding="utf-8")
    back = cs._read_netscape(str(path))
    assert back[0]["name"] == "SID" and back[0]["domain"] == ".youtube.com"


def test_validation_catches_a_logged_out_file(tmp_path):
    """Файл без ключа входа не должен уехать на сервер и затереть рабочий."""
    path = _write(tmp_path, ".instagram.com\tTRUE\t/\tFALSE\t0\tcsrftoken\txyz\n")
    assert cs._looks_valid(path, "sessionid") is False


def test_validation_accepts_a_real_file(tmp_path):
    path = _write(tmp_path, ".instagram.com\tTRUE\t/\tTRUE\t1790000000\tsessionid\tabc\n")
    assert cs._looks_valid(path, "sessionid") is True


def test_every_site_has_a_login_key():
    """У каждой площадки задан ключ, по которому видно, что вход есть."""
    for site in cs.SITES:
        assert site["key"] and site["file"] and site["domains"]


def test_every_site_is_refreshed_by_visiting():
    """Заход на площадку — это и есть обновление сессии, и он нужен всем трём.

    История вопроса: на ПРОТУХШЕМ наборе кук Google заход выглядел разрушительным
    (SID, HSID, APISID пропадали), и YouTube временно исключили. Перепроверка на
    свежих куках показала обратное: не пропало ни одной куки, добавились новые, и
    возрастной ролик после захода открывается. То есть заход не ломает сессию — он
    ломается сам, когда сессии уже нет.
    """
    for site in cs.SITES:
        assert site.get("visit", True) is True
