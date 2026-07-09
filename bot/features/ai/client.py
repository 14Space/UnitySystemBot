"""
Клиент ИИ для команды /ai. Ходит в бесплатные провайдеры (Gemini, Groq) по очереди:
если у одного кончился дневной бесплатный лимит (429) — пробуем следующий. В платные
вызовы не уходим никогда. Провайдеры включаются наличием ключа в .env.

ask(history, lang) -> (text|None, status), где status:
  "ok" — есть ответ; "quota" — бесплатный лимит исчерпан; "error" — сбой;
  "no_provider" — не задан ни один ключ.
"""
import logging

import requests

from bot.config import (
    GEMINI_API_KEY, GROQ_API_KEY, GEMINI_MODEL, GROQ_MODEL,
)

logger = logging.getLogger(__name__)

_LANG_NAME = {"ru": "русском", "uk": "українською", "en": "English"}

_SYSTEM = (
    "Ты — дружелюбный ИИ-ассистент внутри Telegram-бота, в духе «Джарвиса»: по делу, "
    "с лёгким характером, без воды. Отвечай кратко и полезно, обычным текстом без "
    "разметки. Если пользователь ответил командой на чьё-то сообщение — считай его "
    "контекстом вопроса."
)


def _system(lang: str) -> str:
    return _SYSTEM + f" Отвечай на языке пользователя ({_LANG_NAME.get(lang, 'русском')})."


def _gemini(history: list[dict], lang: str) -> tuple[str | None, str]:
    contents = [{"role": "model" if m["role"] == "assistant" else "user",
                 "parts": [{"text": m["content"]}]} for m in history]
    body = {
        "system_instruction": {"parts": [{"text": _system(lang)}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": 800, "temperature": 0.7},
    }
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    r = requests.post(url, json=body, timeout=60)
    if r.status_code == 429:
        return None, "quota"
    if r.status_code != 200:
        logger.warning("Gemini %s: %s", r.status_code, r.text[:200])
        return None, "error"
    cand = (r.json().get("candidates") or [])
    if not cand:
        return None, "error"
    text = "".join(p.get("text", "") for p in cand[0].get("content", {}).get("parts", [])).strip()
    return (text, "ok") if text else (None, "error")


def _groq(history: list[dict], lang: str) -> tuple[str | None, str]:
    msgs = [{"role": "system", "content": _system(lang)}]
    msgs += [{"role": "assistant" if m["role"] == "assistant" else "user",
              "content": m["content"]} for m in history]
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={"model": GROQ_MODEL, "messages": msgs, "max_tokens": 800, "temperature": 0.7},
        timeout=60,
    )
    if r.status_code == 429:
        return None, "quota"
    if r.status_code != 200:
        logger.warning("Groq %s: %s", r.status_code, r.text[:200])
        return None, "error"
    text = ((r.json().get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
    return (text, "ok") if text else (None, "error")


def ask(history: list[dict], lang: str = "ru") -> tuple[str | None, str]:
    """Спрашивает у первого доступного провайдера; при исчерпании лимита — у следующего."""
    providers = []
    if GEMINI_API_KEY:
        providers.append(_gemini)
    if GROQ_API_KEY:
        providers.append(_groq)
    if not providers:
        return None, "no_provider"

    last = "error"
    for call in providers:
        try:
            text, status = call(history, lang)
        except Exception:
            logger.exception("Провайдер ИИ упал")
            last = "error"
            continue
        if status == "ok":
            return text, "ok"
        last = status                 # quota/error — пробуем следующего провайдера
    return None, last
