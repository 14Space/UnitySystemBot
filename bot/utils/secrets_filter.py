"""
Маскировка секретов в логах и в тревогах админу.

Зачем. Ключи попадают в текст сами, без чьего-либо умысла: токен бота стоит в адресе
запроса к Telegram, ключ Gemini — в строке запроса, и стоит такому запросу упасть, как
`logger.exception` печатает адрес ЦЕЛИКОМ вместе с ключом. Тревога админу шлёт
`str(exception)` — туда же. Логи потом уезжают в отчёты, скриншоты и переписку с
помощниками, и ключ утекает не через взлом, а через обычную диагностику.

Поэтому маскировка стоит в ДВУХ местах: фильтром на корневом логгере (ловит всё, что
пишется в лог, включая чужие библиотеки) и функцией mask() — ей прогоняем текст перед
отправкой в Telegram.

Формат замены оставляет начало, чтобы по логу можно было понять, о каком ключе речь:
«12345678:AA…» вместо полного токена.
"""
import logging
import re

# Токен бота: «123456789:AAE…». Именно в таком виде он стоит в адресах Bot API —
# причём приклеенным к слову «bot» (…/bot123456789:AAE…/getMe), поэтому обычной
# границы слова тут не хватает и её приходится описывать отдельно.
_BOT_TOKEN = re.compile(r"(?:(?<=bot)|(?<![\w:]))(\d{6,12}):[A-Za-z0-9_-]{20,}")
# Ключ в строке запроса: «?key=…», «&api_key=…», «token=…».
_QUERY_KEY = re.compile(r"((?:api[_-]?key|key|token|access_token)=)[^&\s\"']+",
                        re.IGNORECASE)
# Заголовок с ключом: «Bearer …», «Crypto-Pay-API-Token: …».
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{10,}", re.IGNORECASE)
_PAY_TOKEN = re.compile(r"((?:Crypto-Pay-API-Token|X-Goog-Api-Key)\s*[:=]\s*)[^\s,\"']+",
                        re.IGNORECASE)


def mask(text: str) -> str:
    """Возвращает текст, в котором секреты заменены на «…»."""
    if not text:
        return text
    text = _BOT_TOKEN.sub(lambda m: f"{m.group(1)}:…", text)
    text = _QUERY_KEY.sub(lambda m: f"{m.group(1)}…", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)}…", text)
    text = _PAY_TOKEN.sub(lambda m: f"{m.group(1)}…", text)
    return text


class SecretsFilter(logging.Filter):
    """Фильтр логов: маскирует секреты в сообщении, аргументах и тексте исключения.

    Фильтр, а не форматтер: так он работает при любом обработчике и не зависит от
    того, как настроен вывод. Ставится на КОРНЕВОЙ логгер — тогда под него попадают
    и сообщения сторонних библиотек (aiogram, yt-dlp, requests), а они про наши
    секреты ничего не знают.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: mask(v) if isinstance(v, str) else v
                               for k, v in record.args.items()}
            else:
                record.args = tuple(mask(a) if isinstance(a, str) else a
                                    for a in record.args)
        # Текст исключения печатается отдельно от message — его тоже надо прикрыть.
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            masked = mask(str(exc))
            if masked != str(exc):
                record.exc_info = (record.exc_info[0], _Masked(masked),
                                   record.exc_info[2])
        return True


class _Masked(Exception):
    """Подменённое исключение: тот же текст, но без секретов. Тип теряется, поэтому
    подменяем ТОЛЬКО когда в тексте действительно был секрет."""


def install() -> None:
    """Ставит фильтр на корневой логгер и на уже созданные обработчики."""
    f = SecretsFilter()
    root = logging.getLogger()
    root.addFilter(f)
    for handler in root.handlers:
        handler.addFilter(f)
