"""Вторая группа правок по аудиту: дырки и лимиты.

Каждый тест назван бедой, которую он не даёт вернуться.
"""
import ast
import asyncio
import importlib
import pathlib

import pytest


# --- S2: ссылка ведёт не туда, куда написано ------------------------------

@pytest.mark.parametrize("url", [
    "https://rezka.ag@evil.com/film",          # логин в адресе: шли на evil.com
    "https://tiktok.com.evil.com/video/1",     # чужой домен с нашим внутри
    "https://evil.com/?from=pornhub.com",      # наше слово в параметрах
    "https://notpinterest.com/pin/1",
    "https://youtube.com:8080/watch?v=x",      # нестандартный порт
])
def test_spoofed_links_are_not_ours(url):
    from bot.utils.platform_detector import Platform, detect_platform
    assert detect_platform(url) == Platform.UNKNOWN


@pytest.mark.parametrize("url, expected", [
    ("https://vt.tiktok.com/ZSqnUQ5uy/", "tiktok"),
    ("https://www.tiktok.com/@a/video/1", "tiktok"),
    ("https://rezka.ag/films/x.html", "hdrezka"),
    ("https://hdrezka.me/films/x.html", "hdrezka"),
    ("https://m.youtube.com/watch?v=x", "youtube_video"),
    ("https://on.soundcloud.com/abc", "soundcloud"),
    ("https://www.pinterest.ru/pin/1/", "pinterest"),
    ("https://pin.it/abc", "pinterest"),
    ("https://ru.pornhub.com/view_video.php?viewkey=1", "pornhub"),
    ("https://open.spotify.com/track/1", "spotify"),
    ("https://mobile.twitter.com/a/status/1", "twitter"),
    ("https://www.instagram.com/reel/abc/", "instagram_reel"),
])
def test_real_links_still_recognised(url, expected):
    from bot.utils.platform_detector import detect_platform
    assert detect_platform(url).value == expected


def test_short_link_cannot_lead_off_tiktok():
    from bot.features.download.downloaders import tiktok
    assert tiktok._is_tiktok_url("https://www.tiktok.com/@a/video/1")
    assert not tiktok._is_tiktok_url("https://evil.com/?x=tiktok.com")


# --- S3: браузер открывает только сам сайт --------------------------------

def test_browser_never_opens_a_user_link():
    """Головоломку анти-бота решаем на фиксированном адресе: браузер идёт без
    песочницы и от root, а в контейнере лежат токен бота и куки площадок."""
    source = (pathlib.Path(__file__).resolve().parent.parent
              / "bot/features/download/downloaders/hdrezka_gate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run_with_browser"):
            continue
        # Второй аргумент — адрес. Он обязан быть нашей константой, а не переменной url.
        assert len(node.args) >= 2
        target = node.args[1]
        assert isinstance(target, ast.Name) and target.id == "_WARM_URL", \
            "браузеру снова передают присланный адрес"


# --- S7: лимиты на тяжёлое ------------------------------------------------

def test_long_voice_is_refused():
    from bot.features.transcribe import transcribe

    class Media:
        duration = 10 ** 6

    assert transcribe._too_long(Media()) is True
    Media.duration = 5
    assert transcribe._too_long(Media()) is False


def test_downloads_have_a_size_cap():
    from bot.features.download.downloaders.ytdlp_wrapper import BASE_OPTS
    assert BASE_OPTS.get("max_filesize", 0) > 0


def test_ai_quota_is_taken_before_the_request():
    """Раньше между проверкой лимита и его учётом проходил весь запрос к провайдеру —
    параллельными вопросами суточный лимит обходился как угодно."""
    source = (pathlib.Path(__file__).resolve().parent.parent
              / "bot/features/ai/chat.py").read_text(encoding="utf-8")
    body = source[source.index("async def _answer"):]
    take_at = body.index("_take_turn(message.from_user.id)")
    ask_at = body.index("client.ask")
    assert take_at < ask_at, "лимит ИИ снова засчитывается после ответа"


def test_ai_quota_check_and_count_are_one_step(monkeypatch):
    """Повторный аудит: проверка и учёт стояли отдельными шагами, и пачка параллельных
    вопросов проходила вся – каждый видел «ещё можно»."""
    import asyncio
    from bot.features.ai import chat

    calls = {"n": 0}

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def fake_usage(session, uid):
        await asyncio.sleep(0)            # точка переключения – здесь и была гонка
        return calls["n"], calls["n"]

    async def fake_add(session, uid):
        await asyncio.sleep(0)
        calls["n"] += 1

    monkeypatch.setattr(chat, "ai_usage_today", fake_usage)
    monkeypatch.setattr(chat, "add_ai_usage", fake_add)
    monkeypatch.setattr(chat, "SessionLocal", _Session)
    monkeypatch.setattr(chat, "AI_USER_DAILY_LIMIT", 2)
    monkeypatch.setattr(chat, "AI_DAILY_LIMIT", 100)

    async def run():
        # Замок привязан к циклу событий – на каждый прогон свой.
        monkeypatch.setattr(chat, "_LIMIT_LOCK", asyncio.Lock())
        return await asyncio.gather(*(chat._take_turn(1) for _ in range(5)))

    results = asyncio.run(run())
    assert results.count(True) == 2 and calls["n"] == 2


def test_clicks_are_rate_limited():
    from bot.middlewares.throttle import CallbackThrottleMiddleware
    assert CallbackThrottleMiddleware is not None


def test_per_user_queue_is_bounded():
    from bot.config import THROTTLE_QUEUE_MAX
    assert THROTTLE_QUEUE_MAX > 0


# --- S6: секреты не уезжают в логи ----------------------------------------

@pytest.mark.parametrize("text, hidden", [
    ("GET https://api.telegram.org/bot8843314720:AAHxyzSECRETtoken12345/getMe",
     "AAHxyzSECRETtoken12345"),
    ("?key=AIzaSyVERYSECRETKEY", "AIzaSyVERYSECRETKEY"),
    ("Authorization: Bearer gsk_abcdefghijklmnop", "gsk_abcdefghijklmnop"),
    ("Crypto-Pay-API-Token: 637305:AAg3yUWmpIDKDBR0", "AAg3yUWmpIDKDBR0"),
])
def test_secrets_are_masked(text, hidden):
    from bot.utils.secrets_filter import mask
    assert hidden not in mask(text)


@pytest.mark.parametrize("text", [
    "время 10:30 и число 12345678",
    "скачал 25 файлов за 3.5 секунды",
])
def test_plain_text_is_left_alone(text):
    from bot.utils.secrets_filter import mask
    assert mask(text) == text


def test_gemini_key_is_not_in_the_url():
    source = (pathlib.Path(__file__).resolve().parent.parent
              / "bot/features/ai/client.py").read_text(encoding="utf-8")
    assert "?key={GEMINI_API_KEY}" not in source
    assert "x-goog-api-key" in source


# --- B3: ffmpeg не висит вечно --------------------------------------------

def test_every_subprocess_has_a_timeout():
    """Зависший ffmpeg держал поток и слот очереди до сторожа зависаний — 40 минут."""
    bad = []
    for path in (pathlib.Path(__file__).resolve().parent.parent / "bot").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("run", "check_output", "call")
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and "timeout" not in {kw.arg for kw in node.keywords}):
                bad.append(f"{path.name}:{node.lineno}")
    assert not bad, "запуск без предела времени: " + ", ".join(bad)


# --- B5: один плохой счёт не стопорит остальные ---------------------------

def test_one_bad_invoice_does_not_stop_the_rest():
    source = (pathlib.Path(__file__).resolve().parent.parent
              / "bot/main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    loop = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.For) and isinstance(node.target, ast.Name)
                and node.target.id == "inv"):
            loop = node
    assert loop is not None, "цикл по счетам не найден — тест устарел"
    assert any(isinstance(n, ast.Try) for n in loop.body), \
        "счета снова обрабатываются в общем try: один странный остановит все"


# --- B2: имена файлов уникальны -------------------------------------------

def test_download_paths_are_unique():
    source = (pathlib.Path(__file__).resolve().parent.parent
              / "bot/features/download/downloaders/ytdlp_wrapper.py").read_text(encoding="utf-8")
    assert '"%(id)s_%(height)sp_dl.%(ext)s"' not in source
    assert '"%(title)s_dl.%(ext)s"' not in source


# --- S9: проверка кук идёт тем же путём, что боевой запрос ----------------

def test_instagram_probe_uses_the_instagram_proxy(monkeypatch):
    from bot.features.common import healthcheck as hc
    from bot.utils import net

    monkeypatch.setattr(net, "PROXY_URL", "socks5://общий:1080")
    monkeypatch.setattr(net, "INSTAGRAM_PROXY", "socks5://инстаграм:1080")
    assert hc._home_proxies("instagram")["https"] == "socks5://инстаграм:1080"
    assert hc._home_proxies()["https"] == "socks5://общий:1080"


def test_instagram_cookies_never_go_direct_when_a_tunnel_exists(monkeypatch):
    """Своего туннеля у Instagram нет, а общий есть – запрос с куками всё равно идёт
    через дом. Раньше проверка кук так и делала, а загрузчик шёл напрямую."""
    from bot.features.download.downloaders import instagram
    from bot.utils import net

    monkeypatch.setattr(net, "PROXY_URL", "socks5://общий:1080")
    monkeypatch.setattr(net, "INSTAGRAM_PROXY", "")
    assert instagram._attempts("куки.txt")[0] == ("socks5://общий:1080", "куки.txt")


# --- B1 повторно: премиум по чужой кнопке (S5) ----------------------------

def test_premium_is_checked_for_the_clicker():
    """В группе любой мог открыть чужое меню Premium-пользователя и скачать 4K."""
    root = pathlib.Path(__file__).resolve().parent.parent / "bot/features/download"
    source = "\n".join(p.read_text(encoding="utf-8")
                       for p in [root / "link.py", *sorted((root / "flows").glob("*.py"))])
    assert 'not entry.get("premium")' not in source, \
        "премиум снова проверяется по владельцу меню, а не по нажавшему"
