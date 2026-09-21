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
