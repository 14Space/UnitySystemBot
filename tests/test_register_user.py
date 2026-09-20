"""В базу попадают те, кто обращается к боту, а не все подряд в группе."""
import pytest

from bot.middlewares.routing import is_request


class _MsgFromBot:
    """Сообщение, отправленное ботом (на него отвечают реплаем)."""
    text = "ответ ИИ"
    voice = video_note = caption = reply_to_message = None

    def __init__(self):
        self.from_user = _User(is_bot=True)


class _User:
    def __init__(self, is_bot=False):
        self.is_bot = is_bot


class _Msg:
    def __init__(self, text=None, voice=None, video_note=None, reply_to=None):
        self.text = text
        self.voice = voice
        self.video_note = video_note
        self.caption = None
        self.reply_to_message = reply_to


@pytest.mark.parametrize("message", [
    _Msg(text="/start"),
    _Msg(text="/ai сколько лететь до Марса"),
    _Msg(text="https://youtu.be/dQw4w9WgXcQ"),
    _Msg(text="100$"),
    _Msg(voice=object()),
    _Msg(video_note=object()),
    # Ответ реплаем на сообщение бота — продолжение разговора с ИИ, без команды.
    _Msg(text="и тебе это нравится?", reply_to=_MsgFromBot()),
])
def test_real_requests_are_counted(message):
    assert is_request(message)


@pytest.mark.parametrize("message", [
    _Msg(text="ну что, погнали"),
    # Реплай на сообщение ЧЕЛОВЕКА — чужой разговор, нас не касается.
    _Msg(text="да ну тебя", reply_to=_Msg(text="привет")),
    _Msg(text="ахахах"),
    _Msg(text="https://example.com/статья"),   # ссылка чужой площадки — бот её не берёт
    _Msg(text=""),
    _Msg(),
])
def test_ordinary_chatter_is_ignored(message):
    assert not is_request(message)
