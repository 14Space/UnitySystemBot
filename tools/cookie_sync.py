"""
Свежие куки без ручной возни: браузер, который живёт ради сессий, и заливка на сервер.

ЗАЧЕМ. Куки площадок стареют. Instagram отзывает сессию, увиденную из чужого места;
Google ротирует токены, и выгруженный файл со временем отстаёт от браузера. Пока их
выгружают руками, это «перевыгрузи cookies.txt» раз в несколько дней — и каждый раз
именно тогда, когда что-то уже сломалось.

КАК ЭТО РАБОТАЕТ. Скрипт держит СВОЙ профиль браузера (обычная папка на диске, как у
Chrome). Один раз ты входишь в нём в аккаунты руками — дальше профиль живёт сам:
скрипт по расписанию открывает в нём Instagram и YouTube, площадки видят нормальную
живую сессию и обновляют куки, а он забирает их и кладёт на сервер.

Почему не «прочитать куки из моего Edge». Edge и Chrome со 127-й версии шифруют базу
кук привязкой к самому браузеру (App-Bound Encryption): сторонняя программа их больше
не расшифрует — yt-dlp честно отвечает «Failed to decrypt with DPAPI». Обходить это,
ослабляя защиту браузера, где лежит твоя почта и банк, — плохая цена за удобство.

Почему не «логиниться по паролю». Автоматический вход Instagram считает угоном:
вместо свежих кук получишь проверку личности и блок аккаунта.

ЧТО ЗАПУСКАТЬ.

    python tools/cookie_sync.py --login      # один раз: откроется окно, войди руками
    python tools/cookie_sync.py              # обновить куки и залить на сервер
    python tools/cookie_sync.py --no-upload  # только обновить локально (проверить)

Дальше ставится в планировщик Windows раз в сутки (команда в конце файла).

ВАЖНО ПРО ДОМ. Запускать это нужно НА ДОМАШНЕЙ машине — той, через которую бот ходит
в интернет. Тогда площадки видят сессию с того же адреса, что и боевые запросы бота,
и не считают её подозрительной. Запуск с сервера смысла не имеет: там чужой адрес,
ради ухода от которого всё и затевалось.
"""
import argparse
import http.cookiejar
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("cookie_sync")

# Где живёт профиль браузера. Это настоящий профиль со своими куками — не системный
# Edge, а отдельный, в который входишь один раз.
PROFILE_DIR = os.getenv("COOKIE_PROFILE_DIR",
                        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     "data", "cookie-profile"))
# Куда складывать выгруженные файлы перед отправкой.
OUT_DIR = os.getenv("COOKIE_OUT_DIR", os.path.join(os.path.dirname(PROFILE_DIR), "cookies-out"))

# Сервер, куда заливаем (формат как у ssh). Ключ — тот же, которым ходишь руками.
SERVER = os.getenv("COOKIE_SERVER", "root@213.199.53.85")
SERVER_DIR = os.getenv("COOKIE_SERVER_DIR", "~/UnitySystemBot/data")
SSH_KEY = os.getenv("COOKIE_SSH_KEY", os.path.expanduser("~/.ssh/contabo_key"))

# Площадки: какой адрес открыть, какие куки забрать, как назвать файл и по какой куке
# понять, что вход действительно есть.
SITES = [
    {
        "name": "Instagram",
        "url": "https://www.instagram.com/",
        "domains": (".instagram.com", "instagram.com", "www.instagram.com"),
        "file": "instagram_cookies.txt",
        "key": "sessionid",
    },
    {
        "name": "YouTube",
        "url": "https://www.youtube.com/",
        # Куки Google нужны вместе с youtube-овскими: часть проверок возраста идёт
        # через google.com, и без них ролик 18+ снова попросит подтвердить возраст.
        "domains": (".youtube.com", "youtube.com", "www.youtube.com",
                    ".google.com", "google.com", "accounts.google.com"),
        "file": "youtube_cookies.txt",
        "key": "SID",
    },
    {
        "name": "X (Twitter)",
        "url": "https://x.com/home",
        "domains": (".x.com", "x.com", ".twitter.com", "twitter.com"),
        "file": "x.com_cookies.txt",
        "key": "auth_token",
    },
]


def _netscape(cookies: list[dict]) -> str:
    """Переводит куки Playwright в формат cookies.txt, который понимает yt-dlp.

    Формат древний и придирчивый: семь полей через ТАБУЛЯЦИЮ, шапка обязательна,
    флаги — заглавными TRUE/FALSE. Сессионная кука (без срока) пишется с нулём.
    """
    lines = ["# Netscape HTTP Cookie File",
             "# Выгружено автоматически (tools/cookie_sync.py) — не редактируй руками",
             ""]
    for c in cookies:
        domain = c.get("domain", "")
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = int(c.get("expires") or 0)
        if expires < 0:
            expires = 0
        lines.append("\t".join([domain, include_sub, c.get("path", "/"), secure,
                                str(expires), c.get("name", ""), c.get("value", "")]))
    return "\n".join(lines) + "\n"


def _looks_valid(path: str, key: str) -> bool:
    """Есть ли в файле ключевая кука входа. Пустой или «гостевой» файл заливать нельзя:
    он затрёт на сервере рабочий и всё сломает."""
    jar = http.cookiejar.MozillaCookieJar(path)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception:
        return False
    return any(c.name == key for c in jar)


def _playwright():
    """Playwright с понятным объяснением, если его нет.

    Он не входит в обычный набор бота на домашней машине: сам бот живёт в контейнере,
    а этот скрипт запускается снаружи. Поэтому первый запуск почти всегда упирается
    именно в это.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Не хватает playwright. Поставь его один раз:")
        print("    pip install playwright")
        print("    playwright install chromium")
        raise SystemExit(1)
    return sync_playwright


def _open_browser(playwright, headless: bool):
    """Свой профиль браузера. Chromium ставится вместе с playwright, отдельный браузер
    устанавливать не нужно."""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=headless,
        # Обычный пользовательский агент: с дефолтным «HeadlessChrome» площадки
        # показывают заглушки и просят войти заново.
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
        viewport={"width": 1280, "height": 800},
        locale="ru-RU",
    )


def login() -> int:
    """Первый запуск: открываем окно и ждём, пока человек войдёт во все аккаунты."""
    from playwright.sync_api import sync_playwright

    print("\nСейчас откроется браузер. Войди в аккаунты, которыми пользуется бот:")
    for site in SITES:
        print(f"  • {site['name']}: {site['url']}")
    print("\nДля YouTube входи в ОТДЕЛЬНЫЙ гугл-аккаунт, не в личный.")
    print("Когда закончишь — просто закрой окно браузера (не выходя из аккаунтов).\n")

    with sync_playwright() as pw:
        browser = _open_browser(pw, headless=False)
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto(SITES[0]["url"])
        # Ждём, пока окно закроют: профиль всё это время пишется на диск.
        while True:
            try:
                if not browser.pages:
                    break
                time.sleep(1)
            except Exception:
                break
        try:
            browser.close()
        except Exception:
            pass
    print("Профиль сохранён. Теперь запускай без --login.")
    return 0


def refresh() -> list[str]:
    """Открывает площадки в своём профиле и выгружает куки. Возвращает пути к файлам."""
    sync_playwright = _playwright()

    if not os.path.exists(PROFILE_DIR):
        logger.error("Профиля нет. Сначала запусти: python tools/cookie_sync.py --login")
        return []

    os.makedirs(OUT_DIR, exist_ok=True)
    ready: list[str] = []

    with sync_playwright() as pw:
        browser = _open_browser(pw, headless=True)
        page = browser.pages[0] if browser.pages else browser.new_page()
        for site in SITES:
            try:
                # Заход на страницу — это и есть «обновление» сессии: площадка видит
                # живого пользователя и присылает свежие куки взамен стареющих.
                page.goto(site["url"], wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(3000)
            except Exception as e:
                logger.warning("%s: страница не открылась (%s) — беру что есть",
                               site["name"], type(e).__name__)

            cookies = [c for c in browser.cookies()
                       if any(c.get("domain", "").endswith(d.lstrip(".")) for d in site["domains"])]
            path = os.path.join(OUT_DIR, site["file"])
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(_netscape(cookies))

            if _looks_valid(path, site["key"]):
                logger.info("%s: %d кук, вход на месте", site["name"], len(cookies))
                ready.append(path)
            else:
                logger.error("%s: в профиле нет ключа входа (%s) — файл НЕ поеду заливать. "
                             "Похоже, из аккаунта вышли: запусти --login заново",
                             site["name"], site["key"])
        browser.close()
    return ready


def upload(paths: list[str]) -> bool:
    """Заливает файлы на сервер. Без scp (или без ключа) честно говорит об этом."""
    if not paths:
        logger.error("Заливать нечего")
        return False
    ok = True
    for path in paths:
        cmd = ["scp", "-i", SSH_KEY, path, f"{SERVER}:{SERVER_DIR}/{os.path.basename(path)}"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            logger.info("%s → сервер", os.path.basename(path))
        else:
            ok = False
            logger.error("%s не залился: %s", os.path.basename(path),
                         (res.stderr or "").strip()[:200])
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Свежие куки площадок для бота")
    parser.add_argument("--login", action="store_true",
                        help="открыть браузер и войти в аккаунты (первый запуск)")
    parser.add_argument("--no-upload", action="store_true",
                        help="только обновить локально, на сервер не отправлять")
    args = parser.parse_args()

    if args.login:
        return login()

    paths = refresh()
    if not paths:
        return 1
    if args.no_upload:
        logger.info("Готово (без отправки): %s", OUT_DIR)
        return 0
    return 0 if upload(paths) else 1


if __name__ == "__main__":
    raise SystemExit(main())

# Раз в сутки в 5 утра (выполнить ОДИН раз в PowerShell от имени пользователя):
#
#   schtasks /create /tn "UnitySystem cookies" /tr "python C:\PROJECTS\UnitySystemBot\tools\cookie_sync.py" /sc daily /st 05:00
#
# Проверить, что задача создалась:  schtasks /query /tn "UnitySystem cookies"
# Запустить прямо сейчас:           schtasks /run /tn "UnitySystem cookies"
