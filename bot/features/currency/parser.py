"""
Разбор валютных запросов из свободного текста (чистая функция, без сети).

Ловим «число + валюта» в любом порядке и вплотную (через пробел): «100$», «$100»,
«100 долларов», «5000 грн». Голое слово без числа игнорируем. Символ $ по умолчанию
считаем USD. Число не должно быть приклеено к другому слову/коду, а сообщения со
ссылками игнорируем — чтобы не ловить «валюту» внутри URL.

Дополнительно понимаем:
  • цель перевода: «500 UAH to AED», «5000 грн в шекели», «100 usd eur»;
  • сокращение тысяч/миллионов: «5к», «5k», «5тыс», «5млн», «5кк».

Главный вход: parse(text) -> (amount, from_code, to_code|None) | None
"""
import re

# Поддерживаемые валюты. Порядок = порядок кнопок в /setconfig.
#   names  — как показывать в ответе; symbol — символ (None => покажем код);
#   flag   — флаг страны; aliases — regex-фрагменты для ловли (со склонениями через \w*).
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
    "THB": {"names": {"ru": "батов", "uk": "батів", "en": "THB"}, "symbol": "฿", "flag": "🇹🇭",
            "aliases": [r"thb", r"бат", r"бата", r"батов", r"баты", r"baht"]},
    "KRW": {"names": {"ru": "вон", "uk": "вон", "en": "KRW"}, "symbol": "₩", "flag": "🇰🇷",
            "aliases": [r"krw", r"вон", r"вона", r"воны", r"won"]},
    "AUD": {"names": {"ru": "австрал. долларов", "uk": "австрал. доларів", "en": "AUD"}, "symbol": None, "flag": "🇦🇺",
            "aliases": [r"aud", r"австралийск\w*", r"aussie"]},
    "CAD": {"names": {"ru": "канад. долларов", "uk": "канад. доларів", "en": "CAD"}, "symbol": None, "flag": "🇨🇦",
            "aliases": [r"cad", r"канадск\w*", r"канадськ\w*"]},
    "VND": {"names": {"ru": "донгов", "uk": "донгів", "en": "VND"}, "symbol": "₫", "flag": "🇻🇳",
            "aliases": [r"vnd", r"донг", r"донга", r"донгов", r"dong"]},
    # Кроны: скандинавские ловятся только по коду/прилагательному («крона» уже занята чешской).
    "ISK": {"names": {"ru": "исл. крон", "uk": "ісл. крон", "en": "ISK"}, "symbol": None, "flag": "🇮🇸",
            "aliases": [r"isk", r"исландск\w*", r"ісландськ\w*"]},
    "NOK": {"names": {"ru": "норв. крон", "uk": "норв. крон", "en": "NOK"}, "symbol": None, "flag": "🇳🇴",
            "aliases": [r"nok", r"норвежск\w*", r"норвезьк\w*"]},
    "SEK": {"names": {"ru": "швед. крон", "uk": "швед. крон", "en": "SEK"}, "symbol": None, "flag": "🇸🇪",
            "aliases": [r"sek", r"шведск\w*", r"шведськ\w*"]},
    "GEL": {"names": {"ru": "лари", "uk": "ларі", "en": "GEL"}, "symbol": "₾", "flag": "🇬🇪",
            "aliases": [r"gel", r"лари", r"ларі", r"lari"]},
    "INR": {"names": {"ru": "рупий", "uk": "рупій", "en": "INR"}, "symbol": "₹", "flag": "🇮🇳",
            "aliases": [r"inr", r"рупи\w*", r"rupee\w*"]},
    "AZN": {"names": {"ru": "манатов", "uk": "манатів", "en": "AZN"}, "symbol": "₼", "flag": "🇦🇿",
            "aliases": [r"azn", r"манат\w*", r"manat"]},
    "AMD": {"names": {"ru": "драмов", "uk": "драмів", "en": "AMD"}, "symbol": "֏", "flag": "🇦🇲",
            "aliases": [r"amd", r"драм", r"драмов", r"dram"]},
    "UZS": {"names": {"ru": "сумов", "uk": "сумів", "en": "UZS"}, "symbol": None, "flag": "🇺🇿",
            "aliases": [r"uzs", r"сум", r"сумов", r"узбекск\w*"]},
    "KGS": {"names": {"ru": "сомов", "uk": "сомів", "en": "KGS"}, "symbol": None, "flag": "🇰🇬",
            "aliases": [r"kgs", r"сом", r"сомов", r"кыргызск\w*", r"киргизск\w*"]},
    "HUF": {"names": {"ru": "форинтов", "uk": "форинтів", "en": "HUF"}, "symbol": None, "flag": "🇭🇺",
            "aliases": [r"huf", r"форинт\w*", r"forint"]},
    "BGN": {"names": {"ru": "левов", "uk": "левів", "en": "BGN"}, "symbol": None, "flag": "🇧🇬",
            "aliases": [r"bgn", r"лв", r"лева", r"левов", r"болгарск\w*"]},
}

ORDER = list(CURRENCIES.keys())

# Разделяем алиасы на «словесные» (нужны буквенные границы, чтобы «евро» не ловилось
# в «европа») и «символьные» (границы не нужны — символы и так не буквы).
_LETTER = r"[^\W\d_]"  # любая буква (Unicode), но не цифра/подчёркивание
_word_aliases: list[tuple[re.Pattern, str]] = []
_symbol_aliases: list[tuple[str, str]] = []
for _code, _data in CURRENCIES.items():
    for _a in _data["aliases"]:
        if re.fullmatch(r"[^\w]+|zł", _a):
            _symbol_aliases.append((_a, _code))
        else:
            _word_aliases.append((re.compile(rf"{_a}$", re.IGNORECASE | re.UNICODE), _code))

# Знаки валют лежат в поле symbol — добавляем их и в ловлю (zł уже попал из aliases).
for _code, _data in CURRENCIES.items():
    _sym = _data.get("symbol")
    if _sym and _sym != "zł" and (_sym, _code) not in _symbol_aliases:
        _symbol_aliases.append((_sym, _code))

_symbol_aliases.sort(key=lambda x: -len(x[0]))
# «Код/символ» валюты (usd, $, zł) — в отличие от описательного слова («долларов»).
# Нужно, чтобы «100 usd eur» считать как цель, а «50 австралийских долларов» — нет.
_CODEISH = {c.lower() for c in CURRENCIES} | {s.lower() for s, _ in _symbol_aliases}
_SYM_ALT = "|".join(re.escape(s) for s, _ in _symbol_aliases)
_WORD_ALT = "|".join(al for d in CURRENCIES.values() for al in d["aliases"]
                     if not re.fullmatch(r"[^\w]+", al))

# Валюта: словесная (с буквенными границами) ИЛИ символьная (без границ)
_CUR = rf"(?:(?<!{_LETTER})(?:{_WORD_ALT})(?!{_LETTER})|(?:{_SYM_ALT}))"
# Число (цифры с пробелами/разделителями) + необязательное сокращение (5к, 5млн)
_NUM = r"\d[\d\s.,]*\d|\d"
_SUFFIX = r"(?:кк|kk|млн|тыс\.?|к|k)"

# Перед числом не должно быть буквы/цифры, иначе «ZSCwn7UsD» (кусок ссылки) читается
# как «7 USD». Требуем, чтобы число не было приклеено к другому слову/коду.
_PATTERN = re.compile(
    rf"(?<!\w)(?P<n1>{_NUM})(?P<s1>{_SUFFIX})?\s*(?P<c1>{_CUR})"
    rf"|(?P<c2>{_CUR})\s*(?<!\w)(?P<n2>{_NUM})(?P<s2>{_SUFFIX})?",
    re.IGNORECASE | re.UNICODE,
)

# Сообщения со ссылкой не разбираем (ими занимается скачиватель).
_URL_RE = re.compile(r"https?://|www\.|t\.me/", re.IGNORECASE)

# Необязательная цель перевода после первой пары: «... to AED», «... в шекели», «... eur».
# Связка необязательна; цель берём только если следом реально стоит валюта.
_CONNECTOR = r"(?:->|→|=|to|in|в|на)"
_TARGET_RE = re.compile(rf"^[\s,]*(?P<conn>{_CONNECTOR})?\s*(?P<t>{_CUR})",
                        re.IGNORECASE | re.UNICODE)


def _mult(suffix: str) -> int:
    s = suffix.lower().rstrip(".")
    return 1_000_000 if s in ("кк", "kk", "млн") else 1_000


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
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        frac = s.split(",")[-1]
        if s.count(",") == 1 and len(frac) in (1, 2):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse(text: str) -> tuple[float, str, str | None] | None:
    """Первая пара «число+валюта» -> (сумма, код_из, код_в|None). Иначе None.
    код_в задан, если в сообщении явно указана целевая валюта."""
    if not text or _URL_RE.search(text):
        return None
    m = _PATTERN.search(text)
    if not m:
        return None
    num = m.group("n1") or m.group("n2")
    suf = m.group("s1") or m.group("s2")
    cur = m.group("c1") or m.group("c2")
    amount = _to_number(num)
    if amount is None or amount <= 0:
        return None
    if suf:
        amount *= _mult(suf)
    from_code = _match_code(cur)
    if not from_code:
        return None
    # Необязательная целевая валюта сразу после первой пары. Без связки («to»/«в»)
    # берём цель только если ОБЕ валюты заданы кодом/символом («100 usd eur»), иначе
    # «50 австралийских долларов» ошибочно превратилось бы в «AUD → USD».
    to_code = None
    tm = _TARGET_RE.match(text[m.end():])
    if tm:
        tc = _match_code(tm.group("t"))
        both_codeish = cur.lower() in _CODEISH and tm.group("t").lower() in _CODEISH
        if tc and tc != from_code and (tm.group("conn") or both_codeish):
            to_code = tc
    return amount, from_code, to_code


def fmt_amount(x: float) -> str:
    """Красиво форматирует сумму: тысячи пробелом, дробную часть — только если есть."""
    x = round(x, 2)
    if x == int(x):
        return f"{int(x):,}".replace(",", " ")
    return f"{x:,.2f}".replace(",", " ")
