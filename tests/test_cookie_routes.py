"""Куда уходят куки: проверка по КОДУ, а не по поведению.

Правило одно и очень дорогое: запрос, который несёт нашу сессию, идёт ТОЛЬКО через
домашний канал. Сессию, увиденную из дата-центра, площадка считает угоном и закрывает
вход — и восстановить его снаружи нельзя, только перевыгрузить руками.

За три недели правило нарушалось трижды и каждый раз в новом месте: стартовая проверка
Instagram, скрипт обновления кук, рендер страницы в браузере. Глазами такие места
находятся плохо: они выглядят как обычный запрос. Поэтому проверяем форму кода.

Что требуем:
  • у вызова requests с `cookies=` обязателен `proxies=`;
  • функция, которая отдаёт куки браузеру (`add_cookies`), обязана упоминать прокси.

Исключения нет ни одного: если запросу прокси не нужен, значит ему не нужны и куки.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent / "bot"
FILES = sorted(ROOT.rglob("*.py"))

_REQUESTS = {"get", "post", "put", "head", "request", "delete"}


def _requests_with_cookies(tree: ast.AST) -> list[ast.Call]:
    """Вызовы requests.*, которые несут куки."""
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in _REQUESTS:
            continue
        names = {kw.arg for kw in node.keywords}
        if "cookies" in names:
            found.append(node)
    return found


def _cookie_route_problems(source: str, name: str) -> list[str]:
    tree = ast.parse(source)
    bad = []
    for call in _requests_with_cookies(tree):
        if "proxies" not in {kw.arg for kw in call.keywords}:
            bad.append(f"{name}:{call.lineno}: запрос с куками идёт без прокси")

    # Куки в браузер: сам контекст создаётся строкой выше, поэтому смотрим на функцию
    # целиком — в ней обязано быть слово «proxy» (см. instagram._embed_image_src).
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        gives_cookies = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                            and n.func.attr == "add_cookies" for n in ast.walk(func))
        if not gives_cookies:
            continue
        mentions_proxy = any(
            (isinstance(n, ast.Name) and "PROXY" in n.id.upper())
            or (isinstance(n, ast.Constant) and isinstance(n.value, str) and "proxy" in n.value)
            or (isinstance(n, ast.keyword) and n.arg == "proxy")
            for n in ast.walk(func))
        if not mentions_proxy:
            bad.append(f"{name}:{func.lineno}: куки уходят в браузер без прокси "
                       f"({func.name})")
    return bad


def test_there_are_places_to_check():
    """Страховка от «зелено, потому что ничего не нашли»."""
    total = sum(len(_requests_with_cookies(ast.parse(f.read_text(encoding="utf-8"))))
                for f in FILES)
    assert total >= 3


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_cookies_never_leave_without_the_tunnel(path):
    problems = _cookie_route_problems(path.read_text(encoding="utf-8"), path.name)
    assert not problems, "куки могут уехать с серверного адреса:\n" + "\n".join(problems)


# --- Проверка самой проверки -----------------------------------------------

_BAD_REQUEST = """
def probe(jar):
    return requests.get("https://www.instagram.com/accounts/edit/", cookies=jar, timeout=25)
"""

_GOOD_REQUEST = """
def probe(jar):
    return requests.get("https://www.instagram.com/accounts/edit/", cookies=jar,
                        timeout=25, proxies=_home_proxies())
"""

_BAD_BROWSER = """
def render(browser, code):
    ctx = browser.new_context(user_agent=UA)
    ctx.add_cookies(_pw_cookies())
    return ctx
"""

_GOOD_BROWSER = """
def render(browser, code):
    ctx = browser.new_context(user_agent=UA, proxy={"server": INSTAGRAM_PROXY})
    ctx.add_cookies(_pw_cookies())
    return ctx
"""


@pytest.mark.parametrize("source", [_BAD_REQUEST, _BAD_BROWSER])
def test_rule_catches_direct_cookies(source):
    assert _cookie_route_problems(source, "образец")


@pytest.mark.parametrize("source", [_GOOD_REQUEST, _GOOD_BROWSER])
def test_rule_accepts_the_tunnel(source):
    assert _cookie_route_problems(source, "образец") == []
