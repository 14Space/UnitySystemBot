"""Ежемесячная сверка библиотек и суточная копия секретов (24.09.2026)."""
import zipfile

from bot.features.common import deps_check


def test_requirement_names_skip_comments_versions_and_ytdlp(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("# шапка\naiogram==3.20.0\nyt-dlp>=2025.6.9\n"
                   "requests[socks]==2.34.2   # с прокси\ncurl_cffi>=0.7\n\n",
                   encoding="utf-8")
    assert deps_check.requirement_names(req) == ["aiogram", "requests", "curl_cffi"]


def test_outdated_and_vulnerable_are_reported(monkeypatch):
    monkeypatch.setattr(deps_check.meta, "version",
                        lambda name: {"aiogram": "3.20.0", "Pillow": "12.3.0"}[name])
    answers = {
        "https://pypi.org/pypi/aiogram/json": {"info": {"version": "3.31.0"}},
        "https://pypi.org/pypi/aiogram/3.20.0/json": {"vulnerabilities": [
            {"id": "GHSA-1", "fixed_in": ["3.21.0"]}]},
        "https://pypi.org/pypi/Pillow/json": {"info": {"version": "12.3.0"}},
        "https://pypi.org/pypi/Pillow/12.3.0/json": {"vulnerabilities": []},
    }
    res = deps_check.check(["aiogram", "Pillow"], fetch=answers.__getitem__)
    assert res["outdated"] == [("aiogram", "3.20.0", "3.31.0")]
    assert res["vulnerable"] == [("aiogram", "3.20.0", [("GHSA-1", "3.21.0")])]
    text = deps_check.format_report(res, "ru")
    assert "aiogram: 3.20.0 → 3.31.0" in text and "GHSA-1 → 3.21.0" in text


def test_pypi_outage_does_not_break_the_report(monkeypatch):
    monkeypatch.setattr(deps_check.meta, "version", lambda name: "1.0")

    def down(url):
        raise ConnectionError("PyPI лежит")

    res = deps_check.check(["aiogram"], fetch=down)
    assert res["failed"] == ["aiogram"]
    assert "aiogram" in deps_check.format_report(res, "ru")


def test_monthly_schedule_hits_the_first_at_midnight(monkeypatch):
    from datetime import datetime
    import bot.main as m

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 24, 4, 0, tzinfo=tz)

    monkeypatch.setattr(m, "datetime", _Now)
    assert m._seconds_until_month_day(1, 0) == (6 * 24 + 20) * 3600    # 1 октября 00:00
    assert m._seconds_until_month_day(31, 0) == (5 * 24 + 20) * 3600   # 30 сентября


def test_secrets_archive_has_everything_needed_to_restore(tmp_path, monkeypatch):
    from bot import config
    from bot.features.common import backup

    (tmp_path / "env").write_bytes(b"BOT_TOKEN=1:x\n")
    (tmp_path / "ig.txt").write_text("ig", encoding="utf-8")
    (tmp_path / "yt.txt").write_text("yt", encoding="utf-8")
    (tmp_path / "yt.txt.work").write_text("yt-work", encoding="utf-8")
    wg = tmp_path / "wg"
    wg.mkdir()
    (wg / "wg0.conf").write_text("[Interface]", encoding="utf-8")
    monkeypatch.setattr(config, "SECRETS_ENV_FILE", str(tmp_path / "env"))
    monkeypatch.setattr(config, "INSTAGRAM_COOKIES", str(tmp_path / "ig.txt"))
    monkeypatch.setattr(config, "X_COOKIES", str(tmp_path / "нет-такого.txt"))
    monkeypatch.setattr(config, "YOUTUBE_COOKIES", str(tmp_path / "yt.txt"))
    monkeypatch.setattr(config, "RELAY_STT_SESSION", str(tmp_path / "нет.session"))
    monkeypatch.setattr(config, "WIREGUARD_DIR", str(wg))

    path, names = backup.dump_secrets()
    try:
        assert names == [".env", "ig.txt", "yt.txt", "yt.txt.work", "wireguard/wg0.conf"]
        with zipfile.ZipFile(path) as zf:
            assert zf.read(".env") == b"BOT_TOKEN=1:x\n"
            assert zf.read("wireguard/wg0.conf") == b"[Interface]"
    finally:
        import os
        os.remove(path)


def test_no_secrets_no_archive(tmp_path, monkeypatch):
    from bot import config
    from bot.features.common import backup

    for attr in ("SECRETS_ENV_FILE", "INSTAGRAM_COOKIES", "X_COOKIES", "YOUTUBE_COOKIES",
                 "RELAY_STT_SESSION", "WIREGUARD_DIR"):
        monkeypatch.setattr(config, attr, str(tmp_path / "нет"))
    assert backup.dump_secrets() == (None, [])


def test_waiting_survives_the_clock_change():
    """В Молдове часы переводят дважды в год. Python при вычитании времён одного пояса
    пояс игнорирует, и через перевод ожидание выходило на час короче: 25.10.2026 часы
    отводят назад, и сверка 1 ноября сработала бы в 23:00 31 октября."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import bot.main as m

    z = ZoneInfo("Europe/Chisinau")
    autumn = m._until(datetime(2026, 11, 1, 0, 0, tzinfo=z), datetime(2026, 10, 20, 12, 0, tzinfo=z))
    spring = m._until(datetime(2027, 4, 1, 0, 0, tzinfo=z), datetime(2027, 3, 20, 12, 0, tzinfo=z))
    assert autumn == 277 * 3600       # 11.5 суток + лишний час
    assert spring == 275 * 3600       # 11.5 суток − час


def test_report_first_then_database_and_secrets_as_one_album(tmp_path, monkeypatch):
    """Отчёт – отдельным сообщением, под ним копия базы и архив секретов одним альбомом.
    Файлы за собой убираем."""
    import asyncio
    import bot.main as m

    db, sec = tmp_path / "unitysystem-2026-09-24.db", tmp_path / "unitysystem-secrets.zip"

    async def fake_db():
        db.write_bytes(b"db")
        return str(db)

    def fake_secrets():
        sec.write_bytes(b"zip")
        return str(sec), [".env"]

    monkeypatch.setattr(m, "dump_database", fake_db)
    monkeypatch.setattr(m, "dump_secrets", fake_secrets)
    monkeypatch.setattr(m, "ADMIN_ID", 1)

    class Bot:
        calls = []

        async def send_message(self, chat_id, text, **kw):
            self.calls.append(("text", text))

        async def send_media_group(self, chat_id, media):
            self.calls.append(("album", [x.media.path for x in media],
                               [x.caption for x in media]))

    bot = Bot()
    asyncio.run(m._send_report(bot, "отчёт"))
    assert bot.calls == [("text", "отчёт"), ("album", [str(db), str(sec)], [None, None])]
    assert not db.exists() and not sec.exists()
