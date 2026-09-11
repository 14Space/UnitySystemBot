"""
Замер скорости ботов: наш против чужого, на одних и тех же ссылках.

Зачем: проверка функционала внутри бота отвечает «скачалось ли», но не «стало ли
лучше». Чтобы увидеть второе, нужны цифры до и после правки на живых ссылках –
именно так нашлись и сломанное сжатие (360p вместо 720p), и отставание на TikTok.
Инструмент ручной, к работе бота отношения не имеет и в статистике не появляется.

Что меряет: сколько прошло от отправки ссылки до прихода файла, его размер и
разрешение. Если бот показал кнопки (выбор качества) – жмёт первую и ждёт дальше.

Подготовка (один раз):
    pip install telethon
    python tools/bench_login.py        # вход в Telegram, код вводишь ТЫ

Запуск:
    python tools/bench.py @наш_бот @чужой_бот -- <ссылка1> <ссылка2> ...

Ключи:
    --skip-plain   пропустить проход «без сжатия» (он уже сделан, и повтор пришёл бы
                   из кэша – это показало бы скорость кэша, а не скачивания)
    --skip-rival   не гонять чужого бота (у него свой кэш, повтор соврёт)

По умолчанию три прохода: наш без сжатия, наш со сжатием (тумблер переключается сам
через /setconfig) и чужой бот.

ВАЖНО про честность цифр: кэш есть у обоих ботов. Ссылка, скачанная минуту назад,
придёт за полсекунды, и сравнение станет бессмысленным. Бери свежие непопулярные
ссылки; свой кэш при необходимости чисти через /cleancache.
"""
import asyncio
import os
import re
import sys
import time

from telethon import TelegramClient

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# Файл сессии лежит рядом со скриптом и закрыт в .gitignore: это полный доступ
# к твоему аккаунту Telegram, в репозиторий ему нельзя.
SESSION = os.path.join(HERE, "bench")
ENV = os.path.join(ROOT, ".env")

TIMEOUT = 180        # сколько максимум ждём медиа (сек)
PAUSE_BETWEEN = 8    # пауза между запросами — вежливо к чужому сервису
# Подпись кнопки зависит от языка аккаунта — ловим все три варианта
COMPRESS_LABELS = ("жатие шортс", "тиснення шортс", "ompress shorts")


def env_value(name: str) -> str:
    try:
        with open(ENV, encoding="utf-8") as f:
            for line in f:
                m = re.match(rf"\s*{name}\s*=\s*(.+?)\s*$", line)
                if m:
                    return m.group(1).strip()
    except OSError:
        pass
    return ""


def _media_kind(msg) -> str | None:
    """Что за медиа пришло: video / photo / None (обычный текст)."""
    if getattr(msg, "video", None):
        return "video"
    doc = getattr(msg, "document", None)
    mime = getattr(doc, "mime_type", "") if doc else ""
    if mime.startswith("video/"):
        return "video"
    if getattr(msg, "photo", None):
        return "photo"
    return None


def _meta(msg) -> dict:
    """Размер/разрешение/длительность присланного файла."""
    out = {"mb": None, "wh": None, "sec": None}
    doc = getattr(msg, "document", None) or getattr(getattr(msg, "video", None), "document", None)
    size = getattr(doc, "size", None) if doc else None
    if size:
        out["mb"] = round(size / 1024 / 1024, 2)
    for attr in (getattr(doc, "attributes", None) or []):
        w, h = getattr(attr, "w", None), getattr(attr, "h", None)
        if w and h:
            out["wh"] = f"{w}x{h}"
        d = getattr(attr, "duration", None)
        if d:
            out["sec"] = int(d)
    return out


async def measure(client, bot: str, link: str) -> dict:
    """Один замер: шлём ссылку, ждём медиа."""
    res = {"bot": bot, "link": link, "first": None, "got": None,
           "kind": None, "clicked": False, "meta": {}, "error": None}

    last = await client.get_messages(bot, limit=1)
    last_id = last[0].id if last else 0

    t0 = time.monotonic()
    await client.send_message(bot, link)

    deadline = t0 + TIMEOUT
    clicked_ids = set()
    while time.monotonic() < deadline:
        await asyncio.sleep(0.3)
        try:
            msgs = await client.get_messages(bot, min_id=last_id, limit=30)
        except Exception as e:
            res["error"] = f"{type(e).__name__}: {e}"[:120]
            return res
        for m in reversed(msgs):
            if m.out:
                continue
            if res["first"] is None:
                res["first"] = round(time.monotonic() - t0, 2)
            kind = _media_kind(m)
            if kind:
                res["got"] = round(time.monotonic() - t0, 2)
                res["kind"] = kind
                res["meta"] = _meta(m)
                return res
            if getattr(m, "reply_markup", None) and m.id not in clicked_ids:
                clicked_ids.add(m.id)
                try:
                    await m.click(0)
                    res["clicked"] = True
                except Exception:
                    pass
    res["error"] = f"нет медиа за {TIMEOUT}с"
    return res


def _find_compress(msg):
    """Ищет в клавиатуре кнопку сжатия: (строка, столбец, подпись) или None."""
    markup = getattr(msg, "reply_markup", None)
    if not markup:
        return None
    for r, row in enumerate(markup.rows):
        for c, btn in enumerate(row.buttons):
            if any(lbl in (btn.text or "") for lbl in COMPRESS_LABELS):
                return r, c, btn.text
    return None


async def set_compress(client, bot: str, want: bool) -> str:
    """Ставит тумблер «Сжатие шортс» в нужное положение через /setconfig.

    Ждём именно ту панель, где есть кнопка сжатия, и только среди сообщений новее нашей
    команды — иначе легко схватить старую клавиатуру (выбор качества, слайдшоу) и решить,
    что кнопки нет.
    """
    sent = await client.send_message(bot, "/setconfig")

    panel = found = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not found:
        await asyncio.sleep(1)
        for m in await client.get_messages(bot, min_id=sent.id, limit=10):
            if m.out:
                continue
            hit = _find_compress(m)
            if hit:
                panel, found = m, hit
                break
    if not found:
        return "панель настроек с кнопкой сжатия не пришла за 30с"
    r, c, text = found
    now_on = text.startswith("✅")
    if now_on != want:
        await panel.click(r, c)
        await asyncio.sleep(2)
        again = _find_compress(await client.get_messages(bot, ids=panel.id))
        text = again[2] if again else text
    if text.startswith("✅") != want:
        return f"НЕ УДАЛОСЬ переключить (осталось: {text})"
    return text


async def run_pass(client, bot: str, links: list[str], label: str) -> list[dict]:
    print(f"\n───── {label} ─────", flush=True)
    rows = []
    for link in links:
        r = await measure(client, bot, link)
        r["label"] = label
        rows.append(r)
        meta = r["meta"] or {}
        status = r["error"] or (
            f"{r['kind']} за {r['got']}с"
            f"{' [кнопка]' if r['clicked'] else ''}"
            f" | {meta.get('mb') or '?'}МБ {meta.get('wh') or ''} {meta.get('sec') or ''}"
        )
        print(f"{link[:52]:<54} {status}", flush=True)
        await asyncio.sleep(PAUSE_BETWEEN)
    return rows


def summarize(label: str, rows: list[dict], total: int):
    got = [r for r in rows if r["got"] is not None]
    if not got:
        print(f"{label:<28} медиа не получено ни разу")
        return
    avg = sum(r["got"] for r in got) / len(got)
    mbs = [r["meta"].get("mb") for r in got if r["meta"].get("mb")]
    size = f", средний размер {sum(mbs)/len(mbs):.1f}МБ" if mbs else ""
    print(f"{label:<28} {avg:.1f}с в среднем ({len(got)} из {total}){size}")


async def main():
    argv = sys.argv[1:]
    # Проход «без сжатия» пропускаем, если он уже сделан: повтор пришёл бы из кэша
    # и показал бы скорость кэша, а не скачивания.
    skip_plain = "--skip-plain" in argv
    # Конкурента тоже пропускаем, если он уже прогонялся: у него свой кэш.
    skip_rival = "--skip-rival" in argv
    argv = [a for a in argv if a not in ("--skip-plain", "--skip-rival")]
    if "--" not in argv:
        sys.exit("Использование: python tg_bench3.py @наш @конкурент -- <ссылки...>")
    split = argv.index("--")
    bots, links = argv[:split], argv[split + 1:]
    if len(bots) != 2:
        sys.exit("Нужно ровно два бота: наш и конкурент")
    ours, rival = bots

    api_id, api_hash = env_value("TELEGRAM_API_ID"), env_value("TELEGRAM_API_HASH")
    async with TelegramClient(SESSION, int(api_id), api_hash) as client:
        a = []
        if not skip_plain:
            print("Ставлю «Сжатие шортс» ВЫКЛ:",
                  await set_compress(client, ours, False), flush=True)
            a = await run_pass(client, ours, links, "наш · без сжатия")

        print("\nСтавлю «Сжатие шортс» ВКЛ:", await set_compress(client, ours, True), flush=True)
        b = await run_pass(client, ours, links, "наш · со сжатием")

        c = [] if skip_rival else await run_pass(client, rival, links, f"конкурент {rival}")

        print("\nВозвращаю «Сжатие шортс» ВЫКЛ:",
              await set_compress(client, ours, False), flush=True)

        print("\n=== ИТОГ ===")
        for label, rows in (("наш · без сжатия", a), ("наш · со сжатием", b),
                            (f"конкурент {rival}", c)):
            if rows:
                summarize(label, rows, len(links))


if __name__ == "__main__":
    asyncio.run(main())
