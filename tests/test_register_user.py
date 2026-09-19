"""В базу попадают те, кто обращается к боту, а не все подряд в группе."""
import pytest

from bot.middlewares.routing import is_request


class _Msg:
    def __init__(self, text=None, voice=None, video_note=None):
        self.text = text
        self.voice = voice
        self.video_note = video_note
        self.caption = None


@pytest.mark.parametrize("message", [
    _Msg(text="/start"),
    _Msg(text="/ai сколько лететь до Марса"),
    _Msg(text="https://youtu.be/dQw4w9WgXcQ"),
    _Msg(text="100$"),
    _Msg(voice=object()),
    _Msg(video_note=object()),
])
def test_real_requests_are_counted(message):
    assert is_request(message)


@pytest.mark.parametrize("message", [
    _Msg(text="ну что, погнали"),
    _Msg(text="ахахах"),
    _Msg(text="https://example.com/статья"),   # ссылка чужой площадки — бот её не берёт
    _Msg(text=""),
    _Msg(),
])
def test_ordinary_chatter_is_ignored(message):
    assert not is_request(message)
