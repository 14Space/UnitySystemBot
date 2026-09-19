"""Кнопки HDRezka и слайдшоу TikTok переживают перезапуск бота.

Сообщение с кнопками висит в чате и выглядит живым, а память бота живёт только до
перезапуска. Раньше любое нажатие после деплоя отвечало «ссылка устарела».
"""
import asyncio
import importlib


def _fresh_db(tmp_path, monkeypatch, name):
    import bot.config as config
    import bot.database as db

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / name}")
    return importlib.reload(db)


def test_hdrezka_screen_is_restored_from_the_database(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch, "hr.db")
    from bot.features.download import link
    from bot.features.download.downloaders import hdrezka

    monkeypatch.setattr(link, "SessionLocal", db.SessionLocal)
    link.HDREZKA_STORE.clear()

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
        link.HDREZKA_STORE.clear()          # «перезапуск»: память пуста
        entry = await link._hdrezka_entry("sid1")
        missing = await link._hdrezka_entry("нет-такого")
        await db.engine.dispose()
        return entry, missing

    entry, missing = asyncio.run(scenario())
    assert entry["name"] == "Трон"
    assert entry["url"] == "https://rezka.ag/films/x.html"
    assert entry["chat_id"] == -100 and entry["user_msg_id"] == 7
    assert entry["premium"] is True
    assert entry["translators"] == [(1, "Дубляж")]
    assert missing is None


def test_tiktok_screen_is_restored_with_owner(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch, "tt.db")
    from bot.features.download import link
    from bot.features.download.downloaders import tiktok

    monkeypatch.setattr(link, "SessionLocal", db.SessionLocal)
    link.TIKTOK_STORE.clear()
    monkeypatch.setattr(tiktok, "fetch_tiktok", lambda url: {"id": "777", "kind": "slideshow"})

    async def scenario():
        await db.init_db()
        from bot.database.repository import save_link_stash
        async with db.SessionLocal() as s:
            await save_link_stash(s, "sid2", "https://vt.tiktok.com/x/", -100, 9,
                                  kind="tiktok",
                                  payload={"cache_url": "tt:777", "owner": 42})
        link.TIKTOK_STORE.clear()
        return await link._tiktok_entry("sid2")

    entry = asyncio.run(scenario())
    assert entry["cache_url"] == "tt:777"
    assert entry["owner"] == 42               # чужие нажатия по-прежнему игнорируются
    assert entry["info"]["id"] == "777"


def test_kind_is_checked(tmp_path, monkeypatch):
    """Экран выбора качества не должен подсунуться под кнопку HDRezka."""
    db = _fresh_db(tmp_path, monkeypatch, "kind.db")
    from bot.features.download import link

    monkeypatch.setattr(link, "SessionLocal", db.SessionLocal)
    link.HDREZKA_STORE.clear()

    async def scenario():
        await db.init_db()
        from bot.database.repository import save_link_stash
        async with db.SessionLocal() as s:
            await save_link_stash(s, "sid3", "https://youtu.be/x", 1, 2)   # kind=quality
        return await link._hdrezka_entry("sid3")

    assert asyncio.run(scenario()) is None
