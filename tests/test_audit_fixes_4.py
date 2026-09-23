"""Четвёртая группа правок по аудиту: остатки блокировок, проверка данных, скорость.

Здесь два вида проверок. Поведенческие — там, где есть что запустить. И проверки
ФОРМЫ кода — там, где беда именно в структуре: «этот вызов блокирует поток» или
«эта строка идёт в имя файла без проверки» не воспроизводятся в тесте, но прекрасно
видны в исходнике.
"""
import ast
import asyncio
import importlib
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _func(rel: str, name: str):
    for node in ast.walk(ast.parse(_src(rel))):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} не найдена — тест устарел")


# --- P1: остатки блокировок ----------------------------------------------

def test_series_info_is_read_in_a_thread():
    """У сериала разбор страницы делает ещё один запрос к сайту и умеет ждать между
    повторами. В главном потоке это значило, что бот замирал для ВСЕХ."""
    body = ast.unparse(_func("bot/features/download/link.py", "_handle_hdrezka"))
    assert "to_thread(hdrezka.get_info" in body.replace(", ", ", ").replace(
        "to_thread(hdrezka.get_info, api, url)", "to_thread(hdrezka.get_info"), \
        "get_info снова зовётся в главном потоке"


def test_big_file_copy_never_blocks_the_loop():
    """Копирование гигабайтного файла (когда жёсткая ссылка не вышла) обязано идти
    в отдельном потоке — иначе бот замирает для всех на время записи."""
    from bot.utils import tg_files

    assert hasattr(tg_files, "input_file_async")
    link_src = _src("bot/features/download/link.py")
    assert "tg_files.input_file(" not in link_src, \
        "в асинхронном коде снова синхронная версия"


# --- S10: данные из кнопок и от площадок ---------------------------------

def test_unknown_values_from_buttons_are_refused():
    src = _src("bot/features/config/setconfig.py")
    assert "_SLIDESHOW_MODES" in src, "режим слайдшоу из кнопки не проверяется"
    assert "code not in CURRENCIES" in src, "валюта из кнопки не проверяется"


def test_track_number_from_a_button_is_checked():
    body = ast.unparse(_func("bot/features/download/link.py", "handle_collection_track"))
    assert "isdigit" in body, "номер трека снова берётся без проверки"


@pytest.mark.parametrize("code, ok", [
    ("ABC123_-", True),
    ("../../etc/passwd", False),
    ("%2E%2E%2Fetc", False),          # та же попытка, но закодированная
    ("a/b", True),                    # разбор и так останавливается на косой черте
    ("", False),
])
def test_instagram_post_code_is_sanitised(code, ok):
    """Код поста идёт прямо в имя файла: косая черта означала бы запись не туда."""
    from bot.features.download.downloaders.instagram import _shortcode

    url = f"https://www.instagram.com/p/{code}/"
    assert (_shortcode(url) is not None) is ok


@pytest.mark.parametrize("value, expected", [
    ("7688366105840192801", "7688366105840192801"),
    ("../../etc/passwd", "tiktok"),
    ("", "tiktok"),
    (None, "tiktok"),
])
def test_tiktok_id_from_a_foreign_service_is_sanitised(value, expected):
    from bot.features.download.downloaders.tiktok import _safe_id
    assert _safe_id(value) == expected


@pytest.mark.parametrize("code, ok", [("ru", True), ("pt-br", True), ("../x", False)])
def test_subtitle_language_code_is_checked(code, ok):
    from bot.features.download.downloaders.hdrezka import _SUB_CODE_RE
    assert bool(_SUB_CODE_RE.match(code)) is ok


def test_payment_checks_amount_and_currency():
    """Счёт живёт в чате вечно: подняв вчерашнее сообщение, можно было оплатить
    премиум по старой цене."""
    body = ast.unparse(_func("bot/features/common/payment.py", "pre_checkout"))
    assert "total_amount" in body and "currency" in body


# --- P2 и P3: скорость ---------------------------------------------------

def test_thread_pool_is_ours_and_big_enough():
    """По умолчанию потоков «ядра + 4» (на сервере 8), а одних загрузок бывает восемь."""
    from bot.config import THREAD_POOL_SIZE

    assert THREAD_POOL_SIZE >= 16
    assert "set_default_executor" in _src("bot/main.py")


def test_chat_settings_are_cached(tmp_path, monkeypatch):
    """Выключенные функции читаются на КАЖДОЕ сообщение, а меняются раз в месяц."""
    import bot.config as config
    import bot.database as db

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    db = importlib.reload(db)

    async def scenario():
        await db.init_db()
        import bot.database.repository as repo
        importlib.reload(repo)

        reads = []
        async with db.SessionLocal() as s:
            await repo.set_feature(s, 777, "currency", enable=False)

            original = repo.select

            def counting_select(*a, **kw):
                reads.append(1)
                return original(*a, **kw)

            monkeypatch.setattr(repo, "select", counting_select)
            first = await repo.get_disabled_features(s, 777)
            after_cache = len(reads)
            await repo.get_disabled_features(s, 777)      # второе чтение — из памяти
            cached_reads = len(reads) - after_cache

            # Изменили настройку — кэш обязан сброситься, иначе тумблер «залипнет».
            await repo.set_feature(s, 777, "currency", enable=True)
            third = await repo.get_disabled_features(s, 777)
        await db.engine.dispose()
        return first, cached_reads, third

    first, cached_reads, third = asyncio.run(scenario())
    assert first == {"currency"}
    assert cached_reads == 0, "второе чтение снова пошло в базу"
    assert third == set(), "после смены настройки кэш не сбросился"


def test_frequent_requests_reuse_connections():
    """Опрос платежей идёт каждые 15 секунд: без общей сессии каждый запрос заново
    договаривается о шифровании — это лишний круг до сервера и обратно."""
    from bot.utils import net

    assert net.session() is net.session()
    assert "net.session()" in _src("bot/features/common/cryptopay.py")
    assert "net.session()" in _src("bot/features/download/downloaders/tiktok.py")


# --- Запуск не должен падать из-за косметики -----------------------------

def test_startup_survives_a_cosmetic_failure():
    """23.09.2026 бот не поднимался из-за НАДПИСИ в меню: после череды перезапусков
    Telegram включил ограничение частоты на смену списка команд («Retry in 456
    seconds»), ошибка ушла наверх и уронила запуск. Меню — косметика, и всё, что его
    ставит, обязано быть в try."""
    unguarded = []
    for name in ("_setup_commands", "_setup_profile"):
        node = _func("bot/main.py", name)
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                continue
            if not call.func.attr.startswith("set_my_"):
                continue
            # Ищем try, внутри которого этот вызов лежит.
            inside = any(
                any(call is c for c in ast.walk(stmt))
                for t in ast.walk(node) if isinstance(t, ast.Try)
                for stmt in t.body)
            if not inside:
                unguarded.append(f"{name}:{call.lineno} {call.func.attr}")
    assert not unguarded, "запуск снова может упасть из-за меню: " + ", ".join(unguarded)
