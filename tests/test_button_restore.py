"""Кнопки переживают перезапуск бота и слушаются только того, кто прислал ссылку.

Сообщение с кнопками висит в чате и выглядит живым, а память бота живёт только до
перезапуска. Раньше любое нажатие после деплоя отвечало «ссылка устарела».
"""
import asyncio
import importlib
from types import SimpleNamespace


def _fresh_db(tmp_path, monkeypatch, name):
    import bot.config as config
    import bot.database as db
    from bot.features.download import screens

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / name}")
    db = importlib.reload(db)
    monkeypatch.setattr(screens, "SessionLocal", db.SessionLocal)
    return db


def test_hdrezka_screen_is_restored_from_the_database(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch, "hr.db")
    from bot.features.download.flows import hdrezka as hr_flow, tiktok as tt_flow, music
    from bot.features.download.downloaders import hdrezka

    # Заново открывать страницу по-настоящему не нужно — подменяем поход в интернет.
    monkeypatch.setattr(hdrezka, "open_media", lambda url: "сессия")
    monkeypatch.setattr(hdrezka, "get_info", lambda api, url: {
        "name": "Трон", "is_series": False,
        "translators": [(1, "Дубляж")], "thumbnail": None})

    async def scenario():
        await db.init_db()
        from bot.database.repository import save_link_stash
        async with db.SessionLocal() as s:
            await save_link_stash(s, "sid1", "https://rezka.ag/films/x.html", -100, 7,
                                  premium=True, kind="hdrezka")
        hr_flow.HDREZKA.clear()                # «перезапуск»: память пуста
        entry = await hr_flow.HDREZKA.get("sid1")
        missing = await hr_flow.HDREZKA.get("нет-такого")
        await db.engine.dispose()
        return entry, missing

    entry, missing = asyncio.run(scenario())
    assert entry["name"] == "Трон"
    assert entry["api"] == "сессия"
    assert entry["url"] == "https://rezka.ag/films/x.html"
    assert entry["chat_id"] == -100 and entry["user_msg_id"] == 7
    assert entry["premium"] is True
    assert entry["translators"] == [(1, "Дубляж")]
    assert missing is None


def test_tiktok_screen_is_restored_with_owner(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch, "tt.db")
    from bot.features.download.flows import hdrezka as hr_flow, tiktok as tt_flow, music
    from bot.features.download.downloaders import tiktok

    monkeypatch.setattr(tiktok, "fetch_tiktok", lambda url: {"id": "777", "kind": "slideshow"})

    async def scenario():
        await db.init_db()
        from bot.database.repository import save_link_stash
        async with db.SessionLocal() as s:
            await save_link_stash(s, "sid2", "https://vt.tiktok.com/x/", -100, 9,
                                  kind="tiktok",
                                  payload={"cache_url": "tt:777", "owner": 42})
        tt_flow.TIKTOK.clear()
        entry = await tt_flow.TIKTOK.get("sid2")
        await db.engine.dispose()
        return entry

    entry = asyncio.run(scenario())
    assert entry["cache_url"] == "tt:777"
    assert entry["owner"] == 42               # чужие нажатия по-прежнему игнорируются
    assert entry["info"]["id"] == "777"


def test_kind_is_checked(tmp_path, monkeypatch):
    """Экран выбора качества не должен подсунуться под кнопку HDRezka."""
    db = _fresh_db(tmp_path, monkeypatch, "kind.db")
    from bot.features.download.flows import hdrezka as hr_flow, tiktok as tt_flow, music

    async def scenario():
        await db.init_db()
        from bot.database.repository import save_link_stash
        async with db.SessionLocal() as s:
            await save_link_stash(s, "sid3", "https://youtu.be/x", 1, 2)   # kind=quality
        hr_flow.HDREZKA.clear()
        found = await hr_flow.HDREZKA.get("sid3")
        await db.engine.dispose()
        return found

    assert asyncio.run(scenario()) is None


class _Callback:
    """Нажатие кнопки: кто нажал и что ему ответили."""

    def __init__(self, user_id):
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _message(user_id, chat_id=-100, message_id=5):
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id),
                           chat=SimpleNamespace(id=chat_id), message_id=message_id)


def test_only_the_owner_can_press(tmp_path, monkeypatch):
    """S5: в группе любой мог нажать чужой выбор качества или чужую серию HDRezka.
    Проверка владельца была только у TikTok – теперь она у всех экранов разом."""
    db = _fresh_db(tmp_path, monkeypatch, "owner.db")
    from bot.features.download.screens import Screens

    screens = Screens("quality")

    async def scenario():
        await db.init_db()
        sid = await screens.open(_message(42), "https://youtu.be/x", premium=True,
                                 memory={"info": {"title": "т"}})
        stranger, owner = _Callback(7), _Callback(42)
        got_stranger = await screens.for_click(stranger, sid, "ru")
        got_owner = await screens.for_click(owner, sid, "ru")
        screens.clear()                          # «перезапуск»
        restored = await screens.for_click(_Callback(42), sid, "ru")
        stranger_after = await screens.for_click(_Callback(7), sid, "ru")
        await db.engine.dispose()
        return got_stranger, stranger, got_owner, restored, stranger_after

    got_stranger, stranger, got_owner, restored, stranger_after = asyncio.run(scenario())
    assert got_stranger is None
    assert stranger.answers == [(None, False)]   # тихо: только «часики» убрать
    assert got_owner["info"] == {"title": "т"}
    assert restored["owner"] == 42 and restored["premium"] is True
    assert "info" not in restored                # метаданные живут только в памяти
    assert stranger_after is None                # и после перезапуска кнопка не чужая


def test_collection_survives_a_restart(tmp_path, monkeypatch):
    """Список треков жил только в памяти: после деплоя кнопки альбома молчали."""
    db = _fresh_db(tmp_path, monkeypatch, "coll.db")
    from bot.features.download.flows import hdrezka as hr_flow, tiktok as tt_flow, music

    tracks = [{"title": "Трек", "cache_url": "https://open.spotify.com/track/1",
               "source": "ytsearch1:трек", "meta": None, "fallback_query": None}]

    async def scenario():
        await db.init_db()
        sid = await music.COLLECTIONS.open(_message(42), "https://open.spotify.com/album/1",
                                          saved={"title": "Альбом", "tracks": tracks})
        music.COLLECTIONS.clear()
        entry = await music.COLLECTIONS.get(sid)
        await db.engine.dispose()
        return entry

    entry = asyncio.run(scenario())
    assert entry["tracks"] == tracks
    assert entry["owner"] == 42
