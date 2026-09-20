"""Продолжение разговора с ИИ реплаем, без команды /ai.

Главное здесь — не сломать всё остальное: реплаем отвечают и на скачанное видео, и на
расшифровку, и просто друг другу. В aiogram сработавший обработчик забирает сообщение
себе, поэтому «не наш» случай обязан пропускать апдейт дальше (SkipHandler).
"""
import asyncio

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from bot.features.ai import chat
from bot.middlewares.routing import is_request


class _User:
    def __init__(self, is_bot=False, uid=1):
        self.is_bot = is_bot
        self.id = uid
        self.language_code = "ru"


class _Replied:
    def __init__(self, message_id=10, is_bot=True):
        self.message_id = message_id
        self.from_user = _User(is_bot=is_bot)
        self.text = "ответ ИИ"
        self.caption = None


class _Msg:
    def __init__(self, text="и тебе это нравится?", reply_to=None):
        self.text = text
        self.reply_to_message = reply_to
        self.from_user = _User()
        self.chat = type("Chat", (), {"id": -100, "type": "private"})()
        self.voice = self.video_note = self.caption = None
        self.replies = []

    async def reply(self, text, **kw):
        self.replies.append(text)
        return self


def _call(message):
    return asyncio.run(chat.ai_followup(message))


def test_reply_to_our_ai_answer_continues_the_thread(monkeypatch):
    seen = {}

    async def fake_load(session, message_id):
        return [{"role": "user", "content": "как тебя зовут"},
                {"role": "assistant", "content": "Джарвис"}]

    async def fake_answer(message, history, lang):
        seen["history"] = history

    monkeypatch.setattr(chat, "load_ai_thread", fake_load)
    monkeypatch.setattr(chat, "_ai_disabled", lambda chat_: _false())
    monkeypatch.setattr(chat, "_answer", fake_answer)

    _call(_Msg(reply_to=_Replied()))
    assert [m["content"] for m in seen["history"]] == [
        "как тебя зовут", "Джарвис", "и тебе это нравится?"]


async def _false():
    return False


def test_reply_to_a_human_is_not_ours():
    """Люди отвечают друг другу — бот молчит и не мешает."""
    with pytest.raises(SkipHandler):
        _call(_Msg(reply_to=_Replied(is_bot=False)))


def test_reply_to_other_bot_message_is_skipped(monkeypatch):
    """Реплай на скачанное видео или расшифровку — не разговор с ИИ."""
    async def no_thread(session, message_id):
        return None

    monkeypatch.setattr(chat, "load_ai_thread", no_thread)
    with pytest.raises(SkipHandler):
        _call(_Msg(reply_to=_Replied()))


def test_command_is_left_to_the_command_handler():
    with pytest.raises(SkipHandler):
        _call(_Msg(text="/ai ещё вопрос", reply_to=_Replied()))


def test_reply_to_bot_counts_as_a_request():
    """Иначе такой реплай не попал бы ни в очередь, ни под тумблер настроек."""
    assert is_request(_Msg(reply_to=_Replied()))


def test_reply_with_a_link_is_still_a_download():
    """Ответил ссылкой на сообщение бота — это скачивание, а не вопрос ИИ."""
    from bot.middlewares.routing import _msg_feature
    msg = _Msg(text="https://vt.tiktok.com/ZSq7wBhhs", reply_to=_Replied())
    assert _msg_feature(msg) == "download"
