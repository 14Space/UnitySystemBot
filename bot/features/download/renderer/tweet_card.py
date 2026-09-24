"""
Рендер твита картинкой для постов без скачиваемого медиа (чисто текстовый твит,
а позже — цитаты-твиты).

Основной путь: официальное встраивание X (oEmbed) — сам X рисует свой настоящий
твит (родной шрифт, логотип, дата, лайки), а мы «фотографируем» виджет невидимым
браузером Chromium. Получается практически 1:1 как в X.

Запасной путь: если виджет не загрузился (нет сети, X недоступен) — рисуем свою
простую карточку из данных fxtwitter, чтобы пользователь всё равно получил картинку.

Браузер тяжёлый — запускаем один раз и переиспользуем. Функции синхронные
(Playwright sync API), вызывать через asyncio.to_thread.
"""
import html
import logging
import os
import time
import uuid

import requests

from bot.config import DOWNLOADS_DIR
from bot.utils import pw_thread

logger = logging.getLogger(__name__)

OEMBED = "https://publish.twitter.com/oembed"
HEADERS = {"User-Agent": "Mozilla/5.0"}

def _esc(text: str) -> str:
    return html.escape(text or "")


def render_tweet_card(tweet: dict) -> str:
    """Рисует карточку твита и возвращает путь к PNG. Весь Playwright-рендер уводим на
    единый выделенный поток с ОБЩИМ браузером (нельзя дёргать с разных потоков пула, и
    нельзя поднимать второй sync_playwright() на том же потоке)."""
    return pw_thread.run_with_browser(_render_card, tweet)


def _render_card(browser, tweet: dict) -> str:
    """Сначала пробуем настоящий виджет X, при неудаче — свою карточку.
    Бросает исключение, только если не вышло вообще ничего."""
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    # Хвост уникален на каждый рендер: два человека, приславшие один твит, иначе
    # писали бы в один файл, и уборка первого удаляла бы карточку второго до отправки.
    out_path = os.path.join(DOWNLOADS_DIR, f"x_card_{tweet['id']}_{uuid.uuid4().hex[:8]}.png")
    try:
        return _render_embed(browser, tweet, out_path)
    except Exception:
        logger.warning("Виджет X не отрисовался, рисую свою карточку", exc_info=True)
        return _render_own(browser, tweet, out_path)


# --- Основной путь: настоящий виджет X (oEmbed) ------------------------------

def _render_embed(browser, tweet: dict, out_path: str) -> str:
    o = requests.get(
        OEMBED,
        params={"url": tweet["url"], "theme": "dark", "dnt": "true",
                "maxwidth": 550, "hide_thread": "true"},
        headers=HEADERS, timeout=20,
    )
    o.raise_for_status()
    embed_html = o.json().get("html")
    if not embed_html:
        raise ValueError("oEmbed не вернул html")

    page_html = ("<!doctype html><html><head><meta charset=utf-8>"
                 "<style>body{margin:0;background:#000;}</style></head>"
                 "<body>" + embed_html + "</body></html>")

    page = browser.new_page(device_scale_factor=2, viewport={"width": 600, "height": 1400})
    page.set_default_timeout(20000)
    try:
        page.set_content(page_html, wait_until="load")
        # ждём, пока скрипт X заменит цитату на готовый iframe-виджет
        page.wait_for_selector("iframe#twitter-widget-0", timeout=20000)
        time.sleep(3)  # дать виджету дорисоваться (аватар, шрифты, кнопки)
        # Полный виджет как есть (с датой и панелью кнопок снизу)
        page.locator("iframe#twitter-widget-0").screenshot(path=out_path)
        return out_path
    finally:
        page.close()


# --- Запасной путь: своя простая карточка (тёмная тема) ----------------------

def _quote_block(quote: dict) -> str:
    if not quote:
        return ""
    a = quote["author"]
    return f"""
      <div class="quote">
        <div class="qhead">
          <img class="qavatar" src="{_esc(a['avatar_url'])}" />
          <span class="qname">{_esc(a['name'])}</span>
          <span class="qhandle">@{_esc(a['screen_name'])}</span>
        </div>
        <div class="qtext">{_esc(quote['text'])}</div>
      </div>
    """


def _build_html(tweet: dict) -> str:
    a = tweet["author"]
    quote_html = _quote_block(tweet.get("quote"))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:#000; }}
  #card {{
    width:600px; background:#000; color:#e7e9ea; padding:20px 22px;
    font-family:'Noto Sans','Noto Color Emoji',system-ui,sans-serif;
  }}
  .head {{ display:flex; align-items:center; gap:12px; }}
  .avatar {{ width:48px; height:48px; border-radius:50%; object-fit:cover; background:#333; }}
  .names {{ display:flex; flex-direction:column; line-height:1.2; }}
  .name {{ font-weight:700; font-size:17px; color:#e7e9ea; }}
  .handle {{ font-size:15px; color:#71767b; }}
  .text {{
    margin-top:14px; font-size:23px; line-height:1.4; color:#e7e9ea;
    white-space:pre-wrap; word-wrap:break-word;
  }}
  .quote {{
    margin-top:14px; border:1px solid #2f3336; border-radius:16px; padding:12px 14px;
  }}
  .qhead {{ display:flex; align-items:center; gap:8px; }}
  .qavatar {{ width:22px; height:22px; border-radius:50%; object-fit:cover; background:#333; }}
  .qname {{ font-weight:700; font-size:15px; color:#e7e9ea; }}
  .qhandle {{ font-size:14px; color:#71767b; }}
  .qtext {{ margin-top:6px; font-size:16px; line-height:1.4; color:#e7e9ea;
            white-space:pre-wrap; word-wrap:break-word; }}
</style></head>
<body>
  <div id="card">
    <div class="head">
      <img class="avatar" src="{_esc(a['avatar_url'])}" />
      <div class="names">
        <span class="name">{_esc(a['name'])}</span>
        <span class="handle">@{_esc(a['screen_name'])}</span>
      </div>
    </div>
    <div class="text">{_esc(tweet['text'])}</div>
    {quote_html}
  </div>
</body></html>"""


def _render_own(browser, tweet: dict, out_path: str) -> str:
    page = browser.new_page(device_scale_factor=2, viewport={"width": 650, "height": 800})
    page.set_default_timeout(8000)
    try:
        page.set_content(_build_html(tweet), wait_until="load")
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        page.locator("#card").screenshot(path=out_path)
        return out_path
    finally:
        page.close()
