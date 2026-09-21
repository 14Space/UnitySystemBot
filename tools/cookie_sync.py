r"""
Свежие куки без ручной возни: браузер, который живёт ради сессий, и заливка на сервер.

ЗАЧЕМ. Куки площадок стареют. Instagram отзывает сессию, увиденную из чужого места;
Google ротирует токены, и выгруженный файл со временем отстаёт от браузера. Пока их
выгружают руками, это «перевыгрузи cookies.txt» раз в несколько дней — и каждый раз
именно тогда, когда что-то уже сломалось.

КАК ЭТО РАБОТАЕТ. Скрипт держит СВОЙ профиль браузера (обычная папка на диске, как у
Chrome). В него один раз попадают куки уже вошедшего аккаунта (--import), а дальше
скрипт по расписанию забирает их оттуда и кладёт на сервер — ПРОВЕРИВ, что сессия
действительно жива.

ПОЧЕМУ НЕ «ЗАХОДИТЬ НА САЙТ, ЧТОБЫ ОБНОВИТЬ КУКИ». Так было в первой версии, и это
оказалось ровно тем, от чего мы спасались. 20.09.2026: вечером все три площадки
зелёные, в 22:00 отработало расписание, в 00:01 отчёт красный — Instagram и Google
разлогинились. В профиле после этого не осталось ни sessionid, ни SID: заход из
автоматизированного окна площадка считает угоном сессии и закрывает её.

Проверка накануне, показавшая обратное, была неполной: там был ОДИН заход сразу после
переноса свежих кук. Ломается не первый заход, а привычка ходить — и подтвердилось это
только на живом расписании.

Вывод: сессию нельзя «освежить» снаружи. Её обновляет тот браузер, в котором человек
реально сидит. Наше дело — не мешать ей жить (запросы бота идут через домашний канал,
см. документацию) и вовремя заметить, когда она всё-таки кончится.

Куки в профиль проще всего ПЕРЕНЕСТИ из файлов, выгруженных расширением в обычном
браузере (--import). Входить прямо в этом браузере (--login) тоже можно, но Instagram
показывает вошедшему из автоматизированного окна капчу, а её виджет в таком окне часто
не рисуется вовсе — пустая белая страница. Обходить эту проверку мы не будем и не
должны: рабочие куки всё равно уже есть, их достаточно перенести.

Почему не «прочитать куки из моего Edge». Edge и Chrome со 127-й версии шифруют базу
кук привязкой к самому браузеру (App-Bound Encryption): сторонняя программа их больше
не расшифрует — yt-dlp честно отвечает «Failed to decrypt with DPAPI». Обходить это,
ослабляя защиту браузера, где лежит твоя почта и банк, — плохая цена за удобство.

Почему не «логиниться по паролю». Автоматический вход Instagram считает угоном:
вместо свежих кук получишь проверку личности и блок аккаунта.

ЧТО ЗАПУСКАТЬ (из папки проекта, команды по одной — PowerShell 5.1 не понимает «&&»).

    pip install playwright
    playwright install chromium
    python tools/cookie_sync.py --import C:\Edge\www.instagram.com_cookies.txt C:\Edge\www.youtube.com_cookies.txt
    python tools/cookie_sync.py --no-upload  # проверить, что сессии живы
    python tools/cookie_sync.py              # обновить куки и залить на сервер

    python tools/cookie_sync.py --login      # запасной путь: войти руками в окне

Если «python» не находится — он поставлен как «py»: тогда «py -3.12 tools/cookie_sync.py».

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

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("cookie_sync")

# Чем представляемся при проверке живости: обычный браузер, как у человека.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
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
# «probe» — куда стучаться, чтобы проверить, что сессия ЖИВА, и как понять ответ.
# Это лёгкий HTTP-запрос с куками, без браузера: площадка видит обычное обращение, а
# не автоматизированный браузер, и сессию за такое не закрывает.
SITES = [
    {
        "name": "Instagram",
        "url": "https://www.instagram.com/",
        "domains": (".instagram.com", "instagram.com", "www.instagram.com"),
        "file": "instagram_cookies.txt",
        "key": "sessionid",
        "probe": "https://www.instagram.com/accounts/edit/",
        "alive": lambda r: r.status_code == 200,
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
        # Живость проверяем страницей аккаунта НА YOUTUBE. Заманчивый myaccount.google.com
        # не годится: он отвечает 302 на /intro даже вошедшему, и проверка объявляла
        # живую сессию мёртвой.
        "probe": "https://www.youtube.com/account",
        "alive": lambda r: r.status_code == 200,
    },
    {
        "name": "X (Twitter)",
        "url": "https://x.com/home",
        "domains": (".x.com", "x.com", ".twitter.com", "twitter.com"),
        "file": "x.com_cookies.txt",
        "key": "auth_token",
        # Обычная страница, а не api.x.com: у того свой токен веб-клиента, который
        # живёт отдельной жизнью и отвечал «Invalid or expired token» при совершенно
        # живой сессии. Гостя x.com/home уводит на страницу входа.
        "probe": "https://x.com/home",
        "alive": lambda r: r.status_code == 200,
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


def _cookie_db() -> str:
    """Файл, в котором Chromium хранит куки профиля."""
    return os.path.join(PROFILE_DIR, "Default", "Network", "Cookies")


def _profile_is_busy() -> bool:
    """Профиль уже открыт другим окном браузера?

    Пока он занят, Chromium не запишет в него ничего: куки живут только в памяти
    того окна и пропадают вместе с ним. Именно так выглядела первая версия этого
    скрипта — «перенесено 12 кук, вход на месте», а в следующем запуске пусто.
    """
    db = _cookie_db()
    if not os.path.exists(db):
        return False
    try:
        with open(db, "a+b"):
            return False
    except OSError:
        return True


def _require_free_profile() -> None:
    if not _profile_is_busy():
        return
    print("Профиль сейчас занят другим окном браузера — записать в него ничего нельзя.")
    print("Закрой окно, которое открыл этот скрипт, и запусти команду заново.")
    print("Если окна не видно, оно осталось висеть фоном:")
    print("    Get-Process chrome | Where-Object { $_.Path -like '*ms-playwright*' } | Stop-Process -Force")
    raise SystemExit(1)


def _stored_cookie_names() -> set[str]:
    """Какие куки реально лежат В ФАЙЛЕ профиля (а не в памяти открытого окна).

    Читаем копию: сам файл Chromium держит открытым, да и трогать рабочий не стоит.
    """
    import shutil
    import sqlite3
    import tempfile

    db = _cookie_db()
    if not os.path.exists(db):
        return set()
    copy = os.path.join(tempfile.gettempdir(), "cookie_sync_probe.db")
    try:
        shutil.copyfile(db, copy)
        con = sqlite3.connect(copy)
        names = {row[0] for row in con.execute("SELECT name FROM cookies")}
        con.close()
        return names
    except Exception:
        return set()
    finally:
        try:
            os.remove(copy)
        except OSError:
            pass


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


def _read_netscape(path: str) -> list[dict]:
    """Читает cookies.txt и переводит в вид, который понимает Playwright."""
    jar = http.cookiejar.MozillaCookieJar(path)
    jar.load(ignore_discard=True, ignore_expires=True)
    out = []
    for c in jar:
        out.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path or "/",
            "secure": bool(c.secure),
            # Куке без срока (в файле это 0) проставляем годовой — иначе браузер
            # считает её сессионной и НЕ сохраняет на диск при закрытии. Проверено:
            # так из переноса выпадали YSC, GPS и __Secure-1PSIDRTS, а без них
            # YouTube отвечал «Sign in to confirm your age».
            "expires": float(c.expires) if c.expires else time.time() + 365 * 24 * 3600,
        })
    return out


def import_cookies(paths: list[str]) -> int:
    """Кладёт куки из выгруженных файлов в профиль браузера.

    Это главный способ «войти»: файлы уже выгружены расширением из твоего обычного
    браузера, где ты вошёл как человек — значит и капча, и двухфакторка уже пройдены.
    Профиль после этого считается вошедшим, и дальше сессию поддерживает сам скрипт.
    """
    sync_playwright = _playwright()
    _require_free_profile()
    total = 0

    with sync_playwright() as pw:
        browser = _open_browser(pw, headless=True)
        for path in paths:
            if not os.path.exists(path):
                logger.error("Файла нет: %s", path)
                continue
            try:
                cookies = _read_netscape(path)
            except Exception as e:
                logger.error("Не смог прочитать %s (%s)", path, type(e).__name__)
                continue
            if not cookies:
                logger.error("В файле нет ни одной куки: %s", path)
                continue
            browser.add_cookies(cookies)
            total += len(cookies)
            logger.info("%s: перенесено кук %d", os.path.basename(path), len(cookies))

        # Заходить на площадки здесь НЕ надо: Chromium сбрасывает куки на диск сам
        # при закрытии, а лишний заход в автоматизированном окне Google воспринимает
        # как чужую сессию и вычищает её ключи (проверено на живых куках).
        browser.close()

    if not total:
        logger.error("Ничего не перенесено")
        return 1

    # Проверяем НЕ то, что лежит в памяти закрытого окна, а то, что осталось в файле
    # профиля. Разница принципиальная: именно на ней прошлая версия и врала.
    stored = _stored_cookie_names()
    missing = []
    for site in SITES:
        if site["key"] in stored:
            logger.info("%s: вход сохранён в профиле", site["name"])
        else:
            missing.append(site["name"])
            logger.warning("%s: в профиле нет ключа входа (%s)", site["name"], site["key"])

    if len(missing) == len(SITES):
        logger.error("Ни одна площадка не сохранилась — профиль не принял куки")
        return 1
    logger.info("Готово. Дальше: python tools/cookie_sync.py --no-upload")
    return 0


def login() -> int:
    """Запасной путь: открываем окно и ждём, пока человек войдёт во все аккаунты.

    Работает не везде: Instagram в автоматизированном окне показывает капчу, виджет
    которой часто не отрисовывается — остаётся белая страница. Тогда переноси куки
    из файлов (--import), это и быстрее, и надёжнее.
    """
    sync_playwright = _playwright()
    _require_free_profile()

    print("\nСейчас откроется браузер. Войди в аккаунты, которыми пользуется бот:")
    for site in SITES:
        print(f"  • {site['name']}: {site['url']}")
    print("\nДля YouTube входи в ОТДЕЛЬНЫЙ гугл-аккаунт, не в личный.")
    print("Когда закончишь — просто закрой окно браузера (не выходя из аккаунтов).\n")

    with sync_playwright() as pw:
        browser = _open_browser(pw, headless=False)
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto(SITES[0]["url"])
        # Ждём, пока окно закроют. Опрашиваем сам браузер: когда его закрывают, любое
        # обращение начинает падать — это и есть сигнал. Раньше цикл мог висеть, а
        # вместе с ним оставался жить процесс, который держал профиль заблокированным,
        # и следующий запуск молча писал «в никуда».
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


def _session_is_alive(site: dict, path: str) -> bool:
    """Жива ли сессия на самом деле.

    Наличия куки мало: `sessionid` остаётся в файле и после того, как Instagram закрыл
    сессию. Именно так на сервер уезжали мёртвые куки — формально «ключ входа есть».
    Поэтому спрашиваем саму площадку обычным HTTP-запросом (не браузером: браузер она
    считает угоном, см. шапку файла).

    Не смогли проверить (нет сети, площадка легла) — считаем живой: лучше залить
    рабочие куки, чем не залить из-за собственного сбоя связи.
    """
    probe = site.get("probe")
    if not probe:
        return True
    jar = http.cookiejar.MozillaCookieJar(path)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception:
        return False

    headers = {"User-Agent": _UA}
    try:
        r = requests.get(probe, cookies=jar, headers=headers, timeout=25,
                         allow_redirects=False)
    except Exception as e:
        logger.info("%s: проверить живость не вышло (%s) — считаю, что жива",
                    site["name"], type(e).__name__)
        return True
    ok = site["alive"](r)
    if not ok:
        logger.info("%s: площадка ответила %s — сессия закрыта", site["name"], r.status_code)
    return ok


def refresh() -> list[str]:
    """Забирает куки из профиля и проверяет, что сессии живы. Возвращает пути к файлам.

    По сайтам НЕ ходим (см. шапку файла): заход из автоматизированного окна закрывает
    сессию, а не обновляет её. Задача этого прогона другая — вовремя доставить на сервер
    то, что есть, и не дать уехать туда мёртвым кукам.
    """
    sync_playwright = _playwright()
    _require_free_profile()

    if not os.path.exists(PROFILE_DIR):
        logger.error("Профиля нет. Сначала перенеси куки: "
                     "python tools/cookie_sync.py --import <файлы cookies.txt>")
        return []

    os.makedirs(OUT_DIR, exist_ok=True)
    ready: list[str] = []

    with sync_playwright() as pw:
        browser = _open_browser(pw, headless=True)
        all_cookies = browser.cookies()
        browser.close()

    for site in SITES:
        cookies = [c for c in all_cookies
                   if any(c.get("domain", "").endswith(d.lstrip(".")) for d in site["domains"])]
        path = os.path.join(OUT_DIR, site["file"])
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_netscape(cookies))

        if not _looks_valid(path, site["key"]):
            logger.error("%s: в профиле нет ключа входа (%s). Перенеси свежую выгрузку: "
                         "--import <файл cookies.txt>", site["name"], site["key"])
            continue
        if not _session_is_alive(site, path):
            logger.error("%s: ключ входа есть, но площадка его больше не признаёт. "
                         "Выгрузи куки заново и перенеси: --import <файл cookies.txt>",
                         site["name"])
            continue
        logger.info("%s: %d кук, сессия жива", site["name"], len(cookies))
        ready.append(path)

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
    parser.add_argument("--import", dest="import_files", nargs="+", metavar="ФАЙЛ",
                        help="перенести куки из выгруженных cookies.txt в профиль")
    parser.add_argument("--login", action="store_true",
                        help="открыть браузер и войти руками (запасной путь)")
    parser.add_argument("--no-upload", action="store_true",
                        help="только обновить локально, на сервер не отправлять")
    args = parser.parse_args()

    if args.import_files:
        return import_cookies(args.import_files)

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
