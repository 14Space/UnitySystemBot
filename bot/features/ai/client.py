"""
Клиент ИИ для команды /ai. Ходит в бесплатные провайдеры (Gemini, Groq) по очереди:
если у одного кончился дневной бесплатный лимит (429) — пробуем следующий. В платные
вызовы не уходим никогда. Провайдеры включаются наличием ключа в .env.

Порядок задаётся AI_ORDER (по умолчанию groq → gemini): Groq отвечает за полсекунды,
Gemini на том же вопросе — за 36-46с, но вкусы к ответам у всех разные, поэтому
порядок живёт в .env, а не в коде.

ask(history, lang) -> (text|None, status), где status:
  "ok" — есть ответ; "quota" — бесплатный лимит исчерпан; "error" — сбой;
  "no_provider" — не задан ни один ключ.

Провайдер, который не ответил вовремя или сломался, отставляется на AI_COOLDOWN —
см. _rest. Без этого за тормоза одного платят все: 21.09.2026 Gemini отвечал по
36-46с, и столько же ждал каждый спрашивающий, хотя Groq рядом отвечает за полсекунды.
"""
import logging
import re
import time

import requests

from bot.config import (
    GEMINI_API_KEY, GROQ_API_KEY, GEMINI_MODEL, GROQ_MODEL,
    AI_TIMEOUT, AI_COOLDOWN, AI_ORDER,
)

logger = logging.getLogger(__name__)

_LANG_NAME = {"ru": "русском", "uk": "українською", "en": "English"}

_SYSTEM = (
    "Ты — умный и обаятельный ИИ-собеседник внутри Telegram-бота, в духе «Джарвиса»: "
    "живой, с характером, лёгкой иронией и человеческой интонацией. Пиши так, как "
    "говорит начитанный друг в переписке, а не энциклопедия и не школьный реферат: "
    "естественным языком, живыми фразами, без канцелярита и сухого перечисления. "
    "Держись коротко, как в чате — обычно один-три небольших абзаца; разворачивайся "
    "подробнее, только если явно просят. Будь по делу, но не рублено — пусть ответ "
    "звучит как нормальная человеческая речь; можешь пошутить или вставить уместное "
    "словцо. КРИТИЧЕСКИ ВАЖНО: пиши сплошным обычным текстом. Никакой разметки — не "
    "используй звёздочки (* и **), решётки (#), маркированные и нумерованные списки, "
    "заголовки. Мысли разделяй абзацами, а не списками. Если пользователь ответил "
    "командой на чьё-то сообщение — считай его контекстом вопроса."
)


def _system(lang: str) -> str:
    """Системная подсказка с языком ответа.

    Язык здесь — не настройка интерфейса, а язык ПОСЛЕДНЕГО вопроса (см. chat.py).
    Оговорка про «язык последнего сообщения» нужна на случай, когда разговор идёт на
    двух языках: без неё модель цепляется за язык начала ветки.
    """
    return _SYSTEM + (
        f" Отвечай на языке пользователя ({_LANG_NAME.get(lang, 'русском')}). "
        "Если последнее сообщение написано на другом языке — отвечай на языке "
        "последнего сообщения.")


def _clean(text: str | None) -> str | None:
    """Страховка на случай, если модель всё же вернёт markdown: бот шлёт простым
    текстом, поэтому «звёздочки» и «решётки» видны пользователю как мусор — снимаем их."""
    if not text:
        return text
    text = text.replace("**", "").replace("__", "")             # жирный
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)    # ### заголовки
    text = re.sub(r"^(\s{0,3})[*]\s+", r"\1", text, flags=re.M)  # markdown-маркеры списка «* »
    return text.strip()


def _gemini(history: list[dict], lang: str) -> tuple[str | None, str]:
    contents = [{"role": "model" if m["role"] == "assistant" else "user",
                 "parts": [{"text": m["content"]}]} for m in history]
    body = {
        "system_instruction": {"parts": [{"text": _system(lang)}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": 800, "temperature": 0.85},
    }
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    r = requests.post(url, json=body, timeout=AI_TIMEOUT)
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
        json={"model": GROQ_MODEL, "messages": msgs, "max_tokens": 800,
              "temperature": 0.85, "reasoning_format": "hidden"},
        timeout=AI_TIMEOUT,
    )
    if r.status_code == 429:
        return None, "quota"
    if r.status_code != 200:
        logger.warning("Groq %s: %s", r.status_code, r.text[:200])
        return None, "error"
    text = ((r.json().get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
    return (text, "ok") if text else (None, "error")


# Провайдеры, отставленные до указанного момента (монотонные часы — перевод системного
# времени не должен ни продлевать отдых, ни отменять его).
_resting: dict[str, float] = {}


def _rest(name: str, seconds: int, why: str) -> None:
    """Отставляет провайдера: пусть отдохнёт, а люди пока ходят к соседнему."""
    _resting[name] = time.monotonic() + seconds
    logger.info("ИИ: «%s» отставлен на %dс (%s)", name, seconds, why)


def _awake(name: str) -> bool:
    until = _resting.get(name)
    return until is None or time.monotonic() >= until


def _queue() -> list[tuple[str, object]]:
    """Провайдеры в порядке AI_ORDER — только те, у кого есть ключ.

    Неизвестное имя в настройке пропускаем с предупреждением: опечатка в .env не
    должна оставлять бота вовсе без ИИ.
    """
    known = {"gemini": (_gemini, GEMINI_API_KEY), "groq": (_groq, GROQ_API_KEY)}
    queue = []
    for name in AI_ORDER:
        if name not in known:
            logger.warning("Неизвестный провайдер ИИ в AI_ORDER: %s", name)
            continue
        call, key = known[name]
        if key:
            queue.append((name, call))
    return queue


def ask(history: list[dict], lang: str = "ru") -> tuple[str | None, str]:
    """Спрашивает у первого доступного провайдера; при исчерпании лимита — у следующего.

    Отдыхающих пропускаем. Если отдыхают все — идём к ним всё равно: лучше медленный
    ответ, чем никакого, а отдых на то и отдых, что мог уже помочь.
    """
    providers = _queue()
    if not providers:
        return None, "no_provider"
    queue = [p for p in providers if _awake(p[0])] or providers

    last = "error"
    for name, call in queue:
        try:
            text, status = call(history, lang)
        except requests.Timeout:
            # Не ошибка провайдера, а именно «не успел»: ждать его снова смысла нет.
            _rest(name, AI_COOLDOWN, f"не ответил за {AI_TIMEOUT}с")
            last = "error"
            continue
        except Exception:
            logger.exception("Провайдер ИИ упал")
            _rest(name, AI_COOLDOWN, "сбой")
            last = "error"
            continue
        if status == "ok":
            _resting.pop(name, None)
            return _clean(text), "ok"
        # Кончился лимит — до полуночи UTC он не восстановится, но и запирать
        # провайдера на весь день не станем: лимиты бывают минутные.
        _rest(name, AI_COOLDOWN * (6 if status == "quota" else 1), status)
        last = status                 # quota/error — пробуем следующего провайдера
    return None, last
