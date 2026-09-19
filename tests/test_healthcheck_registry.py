"""Целостность самой проверки функционала.

Списки вроде _TIKTOK_CHECKS ссылаются на пункты ПО НАЗВАНИЮ. Переименовали пункт —
ссылка молча повисает в пустоте: замок перестаёт действовать, лимит площадки начинает
ловиться, и пункты краснеют «сами по себе». Ровно это и случилось с «TikTok аудио».
"""
import pytest

from bot.features.common import healthcheck as hc
from bot.utils.i18n import SUPPORTED

NAMES = [name for name, _platform, _fn, _url in hc._CHECKS]


def test_names_are_unique():
    assert len(NAMES) == len(set(NAMES))


@pytest.mark.parametrize("group", ["_PLAYWRIGHT_CHECKS", "_TIKTOK_CHECKS", "_INSTAGRAM_CHECKS"])
def test_serialized_groups_point_at_real_checks(group):
    unknown = sorted(set(getattr(hc, group)) - set(NAMES))
    assert not unknown, f"{group} ссылается на несуществующие пункты: {unknown}"


def test_every_tiktok_check_is_serialized():
    """tikwm разрешает один запрос в секунду — мимо замка не должен идти ни один."""
    tiktok = [n for n in NAMES if n.startswith("TikTok")]
    assert set(tiktok) == set(hc._TIKTOK_CHECKS)


@pytest.mark.parametrize("lang", sorted(SUPPORTED))
def test_every_check_name_is_translated(lang):
    """Сверяем по словарю, а не по результату: часть названий («YouTube Shorts»)
    на всех языках пишется одинаково, и сравнение с исходной строкой врало бы."""
    from bot.utils.i18n import _CHECK_NAMES
    missing = [n for n in NAMES if lang not in _CHECK_NAMES.get(n, {})]
    assert not missing, f"нет перевода названия пункта на {lang}: {missing}"


@pytest.mark.parametrize("installed, latest, outdated", [
    ("2026.08.30.232658", "2026.09.01.232658", True),
    ("2026.8.30.232658.dev0", "2026.08.30.232658", False),   # одна и та же версия
    ("2026.09.02.000000", "2026.09.01.232658", False),       # мы свежее
])
def test_version_comparison(installed, latest, outdated):
    assert (hc._ver_parts(installed) < hc._ver_parts(latest)) is outdated
