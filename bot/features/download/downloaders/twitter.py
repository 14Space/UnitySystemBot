"""
Скачивание постов X (Twitter): фото, видео, GIF и текст одного твита.

yt-dlp умеет из X только видео (фото вообще не отдаёт), поэтому идём через
публичный API fxtwitter — он бесплатный, без логина и возвращает всё сразу:
чистый текст, автора с аватаркой, список медиа в правильном порядке и вложенный
твит (для цитат-твитов). Сами файлы качаем напрямую по ссылкам с pbs.twimg.com.
"""
import os
import re

import requests

from bot.features.download.downloaders.ytdlp_wrapper import DOWNLOADS_DIR

# i/status/<id> — самый стабильный путь: не зависит от имени автора в ссылке.
API = "https://api.fxtwitter.com/i/status/{id}"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def _status_id(url: str) -> str:
    """Достаёт числовой id твита из любой ссылки X (.../status/<id>)."""
    m = re.search(r"/status/(\d+)", url)
    if not m:
        raise ValueError("Не похоже на ссылку твита")
    return m.group(1)


def _media_list(tweet: dict) -> list[dict]:
    """Медиа твита в исходном порядке: [{'kind': photo|video|gif, 'url': ...}]."""
    items = []
    media = tweet.get("media") or {}
    # media.all хранит все вложения по порядку (фото и видео вперемешку)
    for it in (media.get("all") or []):
        kind = it.get("type")  # 'photo' | 'video' | 'gif'
        url = it.get("url")
        if kind and url:
            items.append({"kind": kind, "url": url})
    return items


def _author(tweet: dict) -> dict:
    a = tweet.get("author") or {}
    return {
        "name": a.get("name") or "",
        "screen_name": a.get("screen_name") or "",
        "avatar_url": a.get("avatar_url") or "",
    }


def get_tweet(url: str) -> dict:
    """
    Возвращает разобранный твит:
      {
        'id', 'url', 'text',
        'author': {'name','screen_name','avatar_url'},
        'media':  [{'kind','url'}, ...],   # пусто, если чисто текст
        'quote':  None | {'author','text','media'},  # вложенный твит (цитата)
      }
    """
    tweet_id = _status_id(url)
    r = requests.get(API.format(id=tweet_id), headers=HEADERS, timeout=30)
    r.raise_for_status()
    payload = r.json()
    tweet = payload.get("tweet")
    if not tweet:
        raise ValueError(payload.get("message") or "Твит не найден")

    quote = None
    q = tweet.get("quote")
    if q:
        quote = {
            "author": _author(q),
            "text": q.get("text") or "",
            "media": _media_list(q),
        }

    return {
        "id": tweet_id,
        "url": tweet.get("url") or url,
        "text": tweet.get("text") or "",
        "author": _author(tweet),
        "media": _media_list(tweet),
        "quote": quote,
    }


def download_media(items: list[dict], tweet_id: str) -> list[dict]:
    """
    Качает медиа твита в файлы. Возвращает [{'kind','path'}, ...] в том же порядке.
    Фото → .jpg (оригинал), видео и gif → .mp4 (прямая ссылка fxtwitter).
    """
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    files = []
    for i, it in enumerate(items, 1):
        kind = it["kind"]
        ext = ".jpg" if kind == "photo" else ".mp4"
        path = os.path.join(DOWNLOADS_DIR, f"x_{tweet_id}_{i}_viaSaver{ext}")
        content = requests.get(it["url"], headers=HEADERS, timeout=180).content
        with open(path, "wb") as f:
            f.write(content)
        files.append({"kind": kind, "path": path})
    return files
