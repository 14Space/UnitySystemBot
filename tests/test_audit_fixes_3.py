"""Третья группа правок по аудиту: надёжность и уборка.

Здесь нет громких дыр — здесь то, что ломается редко и потому особенно неприятно:
пропавшая фоновая задача, потерянный счётчик, файл в памяти целиком, мёртвый код.
"""
import ast
import asyncio
import importlib
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# --- B6: фоновые задачи не исчезают ---------------------------------------

def test_background_tasks_are_referenced():
    """asyncio держит на задачу только СЛАБУЮ ссылку: без своей сильной сборщик
    мусора вправе выбросить пульс, отчёт или опрос платежей прямо посреди ожидания."""
    source = _src("bot/main.py")
    assert "_BACKGROUND" in source and "_background(" in source
    assert "asyncio.create_task(" not in source, \
        "задача снова создаётся без сильной ссылки"


def test_alert_task_is_referenced():
    source = _src("bot/features/common/alerts.py")
    assert "_PENDING" in source, "тревога снова может исчезнуть до отправки"


# --- B6: счётчик не теряет запросы ----------------------------------------

def test_counter_survives_parallel_requests(tmp_path, monkeypatch):
    """Раньше было «прочитал, сложил, записал»: два запроса одной площадки в одну
    секунду читали одно значение и писали одно — один просто терялся."""
    import bot.config as config
    import bot.database as db

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    db = importlib.reload(db)

    async def scenario():
        await db.init_db()
        import bot.database.repository as repo
        importlib.reload(repo)

        async def one():
            async with db.SessionLocal() as s:
                await repo.increment_download(s, "tiktok")

        await asyncio.gather(*[one() for _ in range(10)])
        async with db.SessionLocal() as s:
            stats = await repo.get_stats(s)
        await db.engine.dispose()
        return stats["downloads"].get("tiktok")

    assert asyncio.run(scenario()) == 10


# --- B6: файл не читается в память целиком --------------------------------

def test_downloads_go_straight_to_disk():
    """`requests.get(...).content` на видео — это десятки мегабайт в памяти на каждую
    из шести одновременных загрузок."""
    # Разбором кода, а не поиском по тексту: иначе тест спотыкается о собственные
    # пояснения в комментариях и докстроках, где этот вызов упомянут как «было».
    offenders = []
    for rel in ("bot/features/download/downloaders/tiktok.py",
                "bot/features/download/downloaders/twitter.py",
                "bot/features/download/downloaders/instagram.py",
                "bot/features/download/downloaders/ytdlp_wrapper.py"):
        for node in ast.walk(ast.parse(_src(rel))):
            if not (isinstance(node, ast.Attribute) and node.attr == "content"):
                continue
            call = node.value
            ok_call = isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            if ok_call and call.func.attr in ("get", "post"):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, "файл снова качается в память: " + ", ".join(offenders)


def test_size_limit_stops_a_download(tmp_path, monkeypatch):
    from bot.utils import net
    from bot.utils.limits import FileTooLargeError

    class FakeResponse:
        headers: dict = {}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            for _ in range(10):
                yield b"x" * 1024

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(net.requests, "get", lambda *a, **kw: FakeResponse())
    path = tmp_path / "big.bin"
    with pytest.raises(FileTooLargeError):
        net.fetch_to_file("https://example.com/big", str(path), max_bytes=2048)
    assert not path.exists(), "недокачанный обрубок остался на диске"


def test_declared_size_is_refused_without_downloading(tmp_path, monkeypatch):
    """Сервер честно сказал размер — отказываемся сразу, не тратя канал."""
    from bot.utils import net
    from bot.utils.limits import FileTooLargeError

    pulled = []

    class FakeResponse:
        headers = {"Content-Length": str(10 ** 10)}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            pulled.append(1)
            yield b"x"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(net.requests, "get", lambda *a, **kw: FakeResponse())
    with pytest.raises(FileTooLargeError):
        net.fetch_to_file("https://example.com/big", str(tmp_path / "x"))
    assert not pulled, "начали качать файл, про который заранее известно, что он велик"


# --- B6: мелочи, которые ломаются молча -----------------------------------

def test_tiktok_cache_has_a_lock():
    """Кэш читают и пишут разные потоки, а уборка перебирает словарь целиком."""
    from bot.features.download.downloaders import tiktok
    assert hasattr(tiktok, "_CACHE_LOCK")


def test_messages_without_an_author_are_handled():
    """Анонимный админ группы и посты канала приходят без from_user."""
    assert 'getattr(message, "from_user", None)' in _src("bot/features/download/link.py")
    assert 'getattr(message, "from_user", None)' in _src("bot/features/common/admin.py")


def test_links_in_captions_are_seen():
    """Ссылку часто присылают подписью к пересланному посту — раньше бот её не видел."""
    assert "m.caption" in _src("bot/middlewares/routing.py")
    assert "message.caption" in _src("bot/features/download/link.py")


def test_cookie_copies_live_outside_downloads():
    """Том downloads примонтирован в контейнер Bot API, и копии живой сессии лежали
    там, где им делать нечего (а уборка хвостов их ещё и сносила)."""
    source = _src("bot/features/download/downloaders/instagram.py")
    assert "COOKIE_COPIES_DIR" in source
    assert "disposable(INSTAGRAM_COOKIES, DOWNLOADS_DIR)" not in source


# --- P4: уборка -----------------------------------------------------------

@pytest.mark.parametrize("name", [
    "build_subtitle_keyboard",      # обработчика hrs: нет
    "_looks_blocked",               # не вызывался
    "_BLOCK_MARKERS",
    "def download_tiktok",          # обёртка без единого вызова
    "_download_and_send_audio",     # прослойка, которая только перезывала
])
def test_dead_code_is_gone(name):
    for path in (ROOT / "bot").rglob("*.py"):
        assert name not in path.read_text(encoding="utf-8"), f"{name} снова в {path.name}"


@pytest.mark.parametrize("constant, places", [
    ('GROUP_TYPES = ("group", "supergroup")', 1),
    ("FREE_QUALITY_LIMIT = 720", 1),
    ("MAX_FILE_BYTES = 1_950_000_000", 1),
])
def test_constants_have_a_single_home(constant, places):
    """Один и тот же порог, написанный в двух местах, расходится молча."""
    found = sum(constant in p.read_text(encoding="utf-8")
                for p in (ROOT / "bot").rglob("*.py"))
    assert found == places, f"«{constant}» объявлен в {found} местах"


def test_progress_bar_is_translated():
    from bot.utils.progress_bar import make_progress_bar
    assert "Загрузка" in make_progress_bar(50, "ru")
    assert "Downloading" in make_progress_bar(50, "en")
    assert "Загрузка" not in _src("bot/utils/progress_bar.py").split('"""')[-1]
