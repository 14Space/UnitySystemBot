"""
Ежемесячная сверка библиотек бота с PyPI: что устарело и в чём известны уязвимости.

Зачем. Версии в requirements.txt закреплены намертво – это правильно (очередная
несовместимая сборка не ломает образ на ровном месте), но у этого есть цена: сами они
никогда не обновляются, а в старых версиях со временем находят дыры. Узнать об этом
можно, только если регулярно спрашивать.

Что делаем. 1-го числа каждого месяца в DEPS_CHECK_HOUR:00 по времени админа берём
каждую библиотеку из requirements.txt, смотрим версию, которая РЕАЛЬНО установлена и
работает, и спрашиваем у PyPI: какая версия последняя и нет ли у нашей известных
уязвимостей (PyPI отдаёт их из базы OSV вместе с версией, где исправлено). Итог уходит
админу одним сообщением.

Сами ничего не обновляем: новая версия библиотеки может поменять поведение бота, и
решение за владельцем. Обновление – это правка requirements.txt, тесты и раскатка через
deploy.sh (он не пустит сборку, на которой тесты упали).

yt-dlp не сверяем: он обновляется сам каждый день (см. maintenance.update_ytdlp).
"""
import importlib.metadata as meta
import logging
import pathlib
import re

from bot.utils import net
from bot.utils.i18n import t

logger = logging.getLogger(__name__)

# requirements.txt лежит в корне проекта: локально – рядом с папкой bot, в контейнере –
# в /app (Dockerfile копирует его туда при сборке).
REQUIREMENTS = pathlib.Path(__file__).resolve().parents[3] / "requirements.txt"

# Обновляется сам каждый день – сверять незачем.
_SKIP = {"yt-dlp"}

_PYPI = "https://pypi.org/pypi/{name}/json"
_PYPI_VERSION = "https://pypi.org/pypi/{name}/{version}/json"


def requirement_names(path: pathlib.Path = REQUIREMENTS) -> list[str]:
    """Имена библиотек из requirements.txt (без версий, extras и комментариев)."""
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
        if m and m.group(0).lower() not in _SKIP:
            names.append(m.group(0))
    return names


def _key(version: str):
    """Ключ сравнения версий. packaging точнее, но в образе его может не быть."""
    try:
        from packaging.version import Version
        return Version(version)
    except Exception:
        return tuple(int(x) for x in re.findall(r"\d+", version)[:4])


def _newer(latest: str, installed: str) -> bool:
    try:
        return _key(latest) > _key(installed)
    except TypeError:
        return latest != installed


def check(names: list[str] | None = None, fetch=None) -> dict:
    """Сверяет библиотеки. Возвращает {"outdated": [(имя, стоит, последняя)],
    "vulnerable": [(имя, стоит, [(id, исправлено_в)])], "failed": [имя]}.

    fetch(url) -> dict – для тестов; по умолчанию запрос к PyPI через общую сессию.
    """
    if fetch is None:
        def fetch(url):
            r = net.session().get(url, timeout=20)
            r.raise_for_status()
            return r.json()

    result = {"outdated": [], "vulnerable": [], "failed": []}
    for name in names if names is not None else requirement_names():
        try:
            installed = meta.version(name)
        except meta.PackageNotFoundError:
            continue                    # не установлена (например, под Windows) – нечего сверять
        try:
            latest = fetch(_PYPI.format(name=name))["info"]["version"]
            vulns = fetch(_PYPI_VERSION.format(name=name, version=installed)).get(
                "vulnerabilities") or []
        except Exception:
            logger.warning("Сверка библиотек: не смог спросить PyPI про %s", name,
                           exc_info=True)
            result["failed"].append(name)
            continue
        if _newer(latest, installed):
            result["outdated"].append((name, installed, latest))
        if vulns:
            result["vulnerable"].append((name, installed, [
                (v.get("id") or "?", ", ".join(v.get("fixed_in") or []) or "?")
                for v in vulns]))
    return result


def format_report(res: dict, lang: str = "ru") -> str:
    """Сообщение админу. Обычный текст: имена библиотек и id уязвимостей разметкой не
    являются, а HTML тут только добавил бы поводов сломать отправку."""
    lines = [t("deps_title", lang)]
    if res["vulnerable"]:
        lines += ["", t("deps_vulnerable", lang)]
        for name, installed, items in res["vulnerable"]:
            fixes = "; ".join(f"{vid} → {fixed}" for vid, fixed in items[:5])
            more = f" (+{len(items) - 5})" if len(items) > 5 else ""
            lines.append(f"⚠️ {name} {installed}: {fixes}{more}")
    if res["outdated"]:
        lines += ["", t("deps_outdated", lang)]
        lines += [f"• {name}: {installed} → {latest}" for name, installed, latest in res["outdated"]]
    if not res["vulnerable"] and not res["outdated"]:
        lines.append(t("deps_all_fresh", lang))
    if res["failed"]:
        lines += ["", t("deps_failed", lang, names=", ".join(res["failed"]))]
    if res["vulnerable"] or res["outdated"]:
        lines += ["", t("deps_how", lang)]
    return "\n".join(lines)
