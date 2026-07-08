"""
Разбор валютных запросов из свободного текста (чистая функция, без сети).

Ловим конструкцию «число + валюта» в любом порядке и вплотную (через пробел):
«100$», «$100», «100 долларов», «5000 грн». Голое слово без числа игнорируем —
так бот не влезает в обычную болтовню. Символ $ по умолчанию считаем USD.

Главный вход: parse(text) -> (amount: float, code: str) | None
"""
import re

# Поддерживаемые валюты. Порядок = порядок кнопок в /setconfig.
#   names  — как показывать в ответе (родительный падеж мн.ч. читается при любом
#            числе: «95 евро», «25 шекелей»);
#   symbol — символ для источника в ответе (None — покажем код);
#   aliases — regex-фрагменты для ловли (коды, символы, слова со склонениями через \w*).
CURRENCIES: dict[str, dict] = {
    "USD": {"names": {"ru": "долларов", "uk": "доларів", "en": "USD"}, "symbol": "$", "flag": "🇺🇸",
            "aliases": [r"usd", r"доллар\w*", r"долар\w*", r"dollar\w*", r"бакс\w*", r"buck\w*"]},
    "EUR": {"names": {"ru": "евро", "uk": "євро", "en": "EUR"}, "symbol": "€", "flag": "🇪🇺",
            "aliases": [r"eur", r"евро", r"євро", r"euro\w*"]},
    "UAH": {"names": {"ru": "гривен", "uk": "гривень", "en": "UAH"}, "symbol": "₴", "flag": "🇺🇦",
            "aliases": [r"uah", r"грн", r"гривн\w*", r"гривен", r"гривень", r"hryvni\w*"]},
    "RUB": {"names": {"ru": "рублей", "uk": "рублів", "en": "RUB"}, "symbol": "₽", "flag": "🇷🇺",
            "aliases": [r"rub", r"руб", r"рубл\w*", r"ruble\w*", r"rouble\w*"]},
    "GBP": {"names": {"ru": "фунтов", "uk": "фунтів", "en": "GBP"}, "symbol": "£", "flag": "🇬🇧",
            "aliases": [r"gbp", r"фунт\w*", r"pound\w*"]},
    "PLN": {"names": {"ru": "злотых", "uk": "злотих", "en": "PLN"}, "symbol": "zł", "flag": "🇵🇱",
            "aliases": [r"pln", r"zł", r"злот\w*", r"zloty\w*"]},
    "ILS": {"names": {"ru": "шекелей", "uk": "шекелів", "en": "ILS"}, "symbol": "₪", "flag": "🇮🇱",
            "aliases": [r"ils", r"шекел\w*", r"shekel\w*"]},
    "KZT": {"names": {"ru": "тенге", "uk": "тенге", "en": "KZT"}, "symbol": "₸", "flag": "🇰🇿",
            "aliases": [r"kzt", r"тенге", r"tenge"]},
    "TRY": {"names": {"ru": "лир", "uk": "лір", "en": "TRY"}, "symbol": "₺", "flag": "🇹🇷",
            "aliases": [r"try", r"лир\w*", r"lira\w*", r"lir\w*"]},
    "JPY": {"names": {"ru": "иен", "uk": "єн", "en": "JPY"}, "symbol": "¥", "flag": "🇯🇵",
            "aliases": [r"jpy", r"иен\w*", r"йен\w*", r"єн\w*", r"yen\w*"]},
    "CNY": {"names": {"ru": "юаней", "uk": "юанів", "en": "CNY"}, "symbol": None, "flag": "🇨🇳",
            "aliases": [r"cny", r"юан\w*", r"yuan\w*"]},
    "CHF": {"names": {"ru": "франков", "uk": "франків", "en": "CHF"}, "symbol": "₣", "flag": "🇨🇭",
            "aliases": [r"chf", r"франк\w*", r"franc\w*"]},
    "MDL": {"names": {"ru": "молд. леев", "uk": "молд. леїв", "en": "MDL"}, "symbol": None, "flag": "🇲🇩",
            "aliases": [r"mdl", r"лей", r"лея", r"леев", r"леїв", r"leu", r"lei", r"молдавск\w*"]},
    "DKK": {"names": {"ru": "датск. крон", "uk": "датськ. крон", "en": "DKK"}, "symbol": None, "flag": "🇩🇰",
            "aliases": [r"dkk", r"датск\w*", r"krone\w*", r"kroner"]},
    "BYN": {"names": {"ru": "бел. рублей", "uk": "біл. рублів", "en": "BYN"}, "symbol": None, "flag": "🇧🇾",
            "aliases": [r"byn", r"белорусск\w*", r"білорусь\w*"]},
    "RON": {"names": {"ru": "рум. леев", "uk": "рум. леїв", "en": "RON"}, "symbol": None, "flag": "🇷🇴",
            "aliases": [r"ron", r"румынск\w*", r"румунськ\w*"]},
    "AED": {"names": {"ru": "дирхамов", "uk": "дирхамів", "en": "AED"}, "symbol": None, "flag": "🇦🇪",
            "aliases": [r"aed", r"дирхам\w*", r"dirham\w*"]},
    # Чешская крона: общий «kr» и «крона» ловятся сюда (датская — только по коду/«датск»).
    "CZK": {"names": {"ru": "чеш. крон", "uk": "чеськ. крон", "en": "CZK"}, "symbol": None, "flag": "🇨🇿",
            "aliases": [r"czk", r"kr", r"крон\w*", r"чешск\w*", r"чеськ\w*", r"korun\w*"]},
}

ORDER = list(CURRENCIES.keys())

# Разделяем алиасы на «словесные» (нужны буквенные границы, чтобы «евро» не ловилось
# в «европа») и «символьные» (границы не нужны — символы и так не буквы).
_LETTER = r"[^\W\d_]"  # любая буква (Unicode), но не цифра/подчёркивание
_word_aliases: list[tuple[re.Pattern, str]] = []
_symbol_aliases: list[tuple[str, str]] = []
for _code, _data in CURRENCIES.items():
    for _a in _data["aliases"]:
        if re.fullmatch(r"[^\w]+|zł", _a):  # символы (в т.ч. zł как исключение — буквенный, но короткий знак)
            _symbol_aliases.append((_a, _code))
        else:
            _word_aliases.append((re.compile(rf"{_a}$", re.IGNORECASE | re.UNICODE), _code))

# Знаки валют лежат в поле symbol (для вывода) — добавляем их и в ловлю. zł уже
# попал из aliases, поэтому его пропускаем, чтобы не задваивать.
for _code, _data in CURRENCIES.items():
    _sym = _data.get("symbol")
    if _sym and _sym != "zł" and (_sym, _code) not in _symbol_aliases:
        _symbol_aliases.append((_sym, _code))

# Символы, отсортированные по длине (длинные раньше — на случай «zł»)
_symbol_aliases.sort(key=lambda x: -len(x[0]))
_SYM_ALT = "|".join(re.escape(s) for s, _ in _symbol_aliases)
_WORD_ALT = "|".join(a for a in
                     [al for d in CURRENCIES.values() for al in d["aliases"]
                      if not re.fullmatch(r"[^\w]+", al)])

# Валюта: словесная (с буквенными границами) ИЛИ символьная (без границ)
_CUR = rf"(?:(?<!{_LETTER})(?:{_WORD_ALT})(?!{_LETTER})|(?:{_SYM_ALT}))"
# Число: цифры с возможными пробелами/разделителями тысяч и дробной частью
_NUM = r"\d[\d\s .,]*\d|\d"

# Перед числом не должно быть буквы, иначе «ZSCwn7UsD» (кусок ссылки) читается как
# «7 USD». Требуем, чтобы число не было приклеено к буквам.
_PATTERN = re.compile(
    rf"(?<!\w)(?P<n1>{_NUM})\s*(?P<c1>{_CUR})"
    rf"|(?P<c2>{_CUR})\s*(?<!\w)(?P<n2>{_NUM})",
    re.IGNORECASE | re.UNICODE,
)

# Если в сообщении есть ссылка — это точно не запрос на конвертацию (ею занимается
# скачиватель). Не разбираем такой текст вовсе, чтобы не ловить «валюту» внутри URL.
_URL_RE = re.compile(r"https?://|www\.|t\.me/", re.IGNORECASE)


def _match_code(token: str) -> str | None:
    """Определяет код валюты по пойманному фрагменту («долларов» -> USD, «$» -> USD)."""
    t = token.strip()
    for sym, code in _symbol_aliases:
        if t.lower() == sym.lower():
            return code
    for pat, code in _word_aliases:
        if pat.fullmatch(t):
            return code
    return None


def _to_number(s: str) -> float | None:
    """Парсит число из строки, аккуратно разбираясь с , и . как разделителями."""
    s = re.sub(r"\s", "", s)  # убираем пробелы-разделители тысяч (в т.ч. неразрывные)
    if "," in s and "." in s:
        # десятичным считаем тот разделитель, что стоит правее
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        frac = s.split(",")[-1]
        if s.count(",") == 1 and len(frac) in (1, 2):
            s = s.replace(",", ".")   # 12,5 -> 12.5
        else:
            s = s.replace(",", "")    # 1,000 -> 1000
    try:
        return float(s)
    except ValueError:
        return None


def parse(text: str) -> tuple[float, str] | None:
    """Первая пара «число+валюта» в тексте -> (сумма, код). Иначе None."""
    if not text or _URL_RE.search(text):
        return None
    m = _PATTERN.search(text)
    if not m:
        return None
    num = m.group("n1") or m.group("n2")
    cur = m.group("c1") or m.group("c2")
    amount = _to_number(num)
    if amount is None or amount <= 0:
        return None
    code = _match_code(cur)
    if not code:
        return None
    return amount, code


def fmt_amount(x: float) -> str:
    """Красиво форматирует сумму: тысячи пробелом, дробную часть — только если есть."""
    x = round(x, 2)
    if x == int(x):
        return f"{int(x):,}".replace(",", " ")
    return f"{x:,.2f}".replace(",", " ")
