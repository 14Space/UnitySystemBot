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


def test_nobody_is_visited_anymore():
    """По сайтам не ходим вовсе.

    Первая версия «обновляла» куки заходом на страницу. 20.09.2026 это проверилось на
    живом расписании: в 22:00 прогон, в 00:01 отчёт красный, в профиле не осталось ни
    sessionid, ни SID — Instagram и Google сочли заход из автоматизированного окна
    угоном сессии. Сессию нельзя освежить снаружи, её обновляет только тот браузер, в
    котором человек реально сидит.
    """
    for site in cs.SITES:
        assert "visit" not in site
        assert "probe" in site and "alive" in site


def test_alive_rules_are_sane():
    """Проверка живости должна признавать вход только по явному «да»."""
    class _Resp:
        def __init__(self, code):
            self.status_code = code

    for site in cs.SITES:
        assert site["alive"](_Resp(200)) is True
        assert site["alive"](_Resp(302)) is False
        assert site["alive"](_Resp(401)) is False


def test_dead_session_is_detected_even_when_the_cookie_is_there(tmp_path, monkeypatch):
    """Главная ловушка: sessionid остаётся в файле и после того, как сессию закрыли.

    Именно так на сервер уезжали мёртвые куки — формально «ключ входа есть».
    """
    path = _write(tmp_path, ".instagram.com\tTRUE\t/\tTRUE\t1790000000\tsessionid\tabc\n")
    site = next(s for s in cs.SITES if s["name"] == "Instagram")

    class _Resp:
        status_code = 302          # Instagram уводит гостя на вход

    monkeypatch.setattr(cs.requests, "get", lambda *a, **kw: _Resp())
    assert cs._session_is_alive(site, path) is False


def test_live_session_passes(tmp_path, monkeypatch):
    path = _write(tmp_path, ".instagram.com\tTRUE\t/\tTRUE\t1790000000\tsessionid\tabc\n")
    site = next(s for s in cs.SITES if s["name"] == "Instagram")

    class _Resp:
        status_code = 200

    monkeypatch.setattr(cs.requests, "get", lambda *a, **kw: _Resp())
    assert cs._session_is_alive(site, path) is True


def test_our_own_network_failure_does_not_block_upload(tmp_path, monkeypatch):
    """Не смогли проверить — считаем живой: лучше залить рабочие куки, чем не залить
    из-за собственного обрыва связи."""
    path = _write(tmp_path, ".instagram.com\tTRUE\t/\tTRUE\t1790000000\tsessionid\tabc\n")
    site = next(s for s in cs.SITES if s["name"] == "Instagram")

    def boom(*a, **kw):
        raise OSError("сеть легла")

    monkeypatch.setattr(cs.requests, "get", boom)
    assert cs._session_is_alive(site, path) is True
