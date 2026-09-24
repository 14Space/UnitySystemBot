"""Одиночное фото Instagram: откуда берём картинку.

yt-dlp такие посты не умеет вовсе («There is no video in this post»), а эмбед, на
который мы раньше опирались, Instagram гостю больше не отдаёт — вместо страницы
приходит проверка на вход. Поэтому ссылку ищем в HTML самой страницы, с куками.
"""
import pytest

from bot.features.download.downloaders.instagram import _image_from_html


def test_display_url_is_found():
    html = '...{"display_url":"https://scontent.cdninstagram.com/v/abc.jpg"}...'
    assert _image_from_html(html) == "https://scontent.cdninstagram.com/v/abc.jpg"


def test_escaped_json_is_unescaped():
    r"""В JSON внутри HTML символы экранированы: & вместо & и \/ вместо /."""
    html = r'{"display_url":"https:\/\/scontent.cdninstagram.com\/v\/x.jpg?a=1&b=2"}'
    assert _image_from_html(html) == "https://scontent.cdninstagram.com/v/x.jpg?a=1&b=2"


def test_og_image_is_a_fallback():
    html = '<meta property="og:image" content="https://scontent.cdninstagram.com/og.jpg">'
    assert _image_from_html(html) == "https://scontent.cdninstagram.com/og.jpg"


def test_plain_src_is_the_last_resort():
    html = '<img alt="x" "src":"https://scontent.cdninstagram.com/last.jpg" >'
    assert _image_from_html(html) == "https://scontent.cdninstagram.com/last.jpg"


@pytest.mark.parametrize("html", [
    "",
    "<html><body>Войдите, чтобы продолжить</body></html>",
    '<img src="data:image/png;base64,iVBORw0KGgo">',     # заглушка-спиннер
])
def test_nothing_found(html):
    assert _image_from_html(html) is None


def test_file_type_comes_from_content_not_from_extension(tmp_path):
    """Повторный аудит: фото это или видео, решало расширение из метаданных."""
    from bot.utils.files import sniff

    cases = {
        "a.jpg": (b"\xff\xd8\xff\xe0" + b"\0" * 12, ("image", ".jpg")),
        "b.jpg": (b"\0\0\0\x18ftypmp42" + b"\0" * 4, ("video", ".mp4")),
        "c.mp4": (b"RIFF\0\0\0\0WEBPVP8 ", ("image", ".webp")),
        "d.jpg": (b"<!doctype html><h", None),
    }
    for name, (head, expected) in cases.items():
        p = tmp_path / name
        p.write_bytes(head)
        assert sniff(str(p)) == expected, name
