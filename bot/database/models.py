from sqlalchemy import Column, Integer, Float, String, Boolean, DateTime, func
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class User(Base):
    """Пользователь бота"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, unique=True, nullable=False)
    username = Column(String, nullable=True)
    is_premium = Column(Boolean, default=False)
    language = Column(String, default="ru")
    created_at = Column(DateTime, server_default=func.now())
    # Когда человек последний раз ОБРАЩАЛСЯ к боту. Нужно, чтобы отличать живых
    # пользователей от накопленных за всё время: «всего 43» — цифра историческая, в неё
    # попали и те, кто когда-то просто оказался в группе с ботом.
    last_seen = Column(DateTime, nullable=True)


class DownloadStat(Base):
    """Счётчик запросов по платформам (для статистики популярности)"""
    __tablename__ = "download_stats"

    platform = Column(String, primary_key=True)
    count = Column(Integer, default=0)


class MonthlyTraffic(Base):
    """Сколько байт скачано (записано на диск) за месяц — для оценки износа SSD.
    Кэш-переотправки сюда не попадают: они на диск ничего не пишут."""
    __tablename__ = "monthly_traffic"

    month = Column(String, primary_key=True)  # "YYYY-MM" (UTC)
    total_bytes = Column(Integer, default=0)
    files = Column(Integer, default=0)


class ChatSettings(Base):
    """Настройки функций по чату (управляются командой /setconfig).
    disabled_features — какие функции выключены в этом чате, через запятую."""
    __tablename__ = "chat_settings"

    chat_id = Column(Integer, primary_key=True)
    disabled_features = Column(String, default="")
    # Режим слайдшоу TikTok в этой группе: video | photos | ask (по умолчанию video)
    slideshow_mode = Column(String, default="video")
    # Валюты, в которые конвертер переводит в этой группе (коды через запятую)
    currency_targets = Column(String, default="USD,EUR")
    # Слать ли музыку слайдшоу отдельным аудио, когда слайдшоу отдаётся как фото
    # (по умолчанию ДА — срабатывает только для фото-слайдшоу, к видео не применяется)
    audio_track = Column(Boolean, default=True)
    # Брать ли у площадки короткие видео в качестве пониже (быстрее, но менее чётко).
    # NULL = не задано: тогда по умолчанию ВКЛ в группах (упор на скорость) и ВЫКЛ в
    # личке (упор на качество) — решает вызывающий код по типу чата.
    compress_shorts = Column(Boolean, nullable=True, default=None)


class CachedFile(Base):
    """Кэш: URL+качество → file_id Telegram (файл лежит на серверах Telegram)"""
    __tablename__ = "cached_files"

    id = Column(Integer, primary_key=True)
    url_hash = Column(String, unique=True, nullable=False)
    original_url = Column(String, nullable=False)
    file_id = Column(String, nullable=False)
    quality = Column(String, nullable=True)
    # file_id в Telegram привязан к боту, который отправил файл: чужой id невалиден.
    # Поэтому кэш храним по каждому боту отдельно.
    bot_id = Column(Integer, nullable=True)
    cached_at = Column(DateTime, server_default=func.now())


class CheckTiming(Base):
    """История длительности проверок функционала.

    Зачем: проверка отвечает «скачалось ли», но не «не стало ли хуже». 10 сентября
    PornHub из-за нашей же правки поехал с 4 секунд на 48 – и все галочки оставались
    зелёными, потому что файл в итоге приходил. Храня время каждой проверки, мы ловим
    такие провалы автоматически, даже когда заранее не догадались их проверять.
    """
    __tablename__ = "check_timings"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, index=True)
    sec = Column(Float, nullable=False)
    at = Column(DateTime, server_default=func.now())


class Payment(Base):
    """Покупка премиума за звёзды Telegram.

    Зачем хранить: без charge_id вернуть звёзды нельзя — именно этот номер требует
    Telegram у того, кто делает возврат. Плюс без записей не ответить на простой
    вопрос «сколько всего купили».
    """
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    charge_id = Column(String, unique=True, nullable=False)  # номер платежа у Telegram
    stars = Column(Integer, default=0)
    # Чем платили: "stars" или "crypto". Раньше способ был один, и поля не было.
    method = Column(String, default="stars")
    # Сумма в долларах — только для крипты: звёзды в доллары мы не переводим, курс
    # у них свой и задним числом он всё равно был бы выдумкой.
    usd = Column(Float, nullable=True)
    at = Column(DateTime, server_default=func.now())
    # Возврат: премиум снимается, а запись остаётся — чтобы история покупок не врала.
    refunded = Column(Boolean, default=False)


class CryptoInvoice(Base):
    """Счёт, выставленный через Crypto Pay (@CryptoBot).

    Зачем хранить у себя. Об оплате мы узнаём опросом, и спрашивать надо ПРО СВОИ
    неоплаченные счета — иначе пришлось бы перебирать всю историю приложения и
    гадать, какие из них мы уже провели. Плюс запись переживает перезапуск: человек
    заплатил в момент деплоя — премиум всё равно включится.

    Номер счёта выдаёт Crypto Pay, он же первичный ключ: повторно тот же счёт в
    таблицу не попадёт.
    """
    __tablename__ = "crypto_invoices"

    invoice_id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    # active — ждём оплаты, paid — оплачен и премиум выдан, expired — протух.
    status = Column(String, default="active", index=True)
    at = Column(DateTime, server_default=func.now())


class StashedLink(Base):
    """Ссылка, за которой стоит показанный экран выбора качества.

    Кнопки в Telegram живут вечно, а память бота — только до перезапуска. Раньше
    данные о ссылке лежали в памяти, и после каждого обновления бота нажатие на
    вчерашние кнопки отвечало «ссылка устарела». Теперь то же самое лежит в базе.

    Метаданные (info площадки, объект сессии HDRezka) сюда НЕ пишем: это мегабайты
    служебного JSON и живые объекты, которые всё равно не сохранить. Храним лишь то,
    из чего всё остальное добывается заново: саму ссылку и пару чисел.

    kind — какой это экран: «quality» (выбор качества), «hdrezka» (озвучки/сезоны),
    «tiktok» (слайдшоу: видео или фото). От вида зависит, что восстанавливать.
    payload — мелочи этого вида (сезон и серия, номер поста, кто нажал), строкой JSON.
    """
    __tablename__ = "stashed_links"

    id = Column(String, primary_key=True)          # тот самый короткий id из кнопки
    url = Column(String, nullable=False)
    chat_id = Column(Integer, nullable=False)
    user_msg_id = Column(Integer, nullable=False)
    premium = Column(Boolean, default=False)
    kind = Column(String, default="quality")
    payload = Column(String, nullable=True)
    at = Column(DateTime, server_default=func.now())


class AiThread(Base):
    """Ветка разговора с ИИ: ответ бота -> история реплик.

    Уточнения задаются ОТВЕТОМ на сообщение бота, а сообщения в чате живут вечно —
    в отличие от памяти бота. Раньше после перезапуска ответ на вчерашнюю реплику
    начинал разговор с чистого листа, и человек не понимал, почему бот «забыл».
    """
    __tablename__ = "ai_threads"

    message_id = Column(Integer, primary_key=True)   # id ответа бота, на который отвечают
    chat_id = Column(Integer, nullable=False)
    history = Column(String, nullable=False)          # список реплик, строкой JSON
    at = Column(DateTime, server_default=func.now())


class AiUsage(Base):
    """Сколько запросов к ИИ сделал человек за сутки.

    Лимиты держат нас в бесплатном тире провайдера. Раньше счётчик жил в памяти, и
    любой перезапуск бота обнулял его — то есть лимит на день можно было обойти
    просто дождавшись деплоя.
    """
    __tablename__ = "ai_usage"

    day = Column(String, primary_key=True)            # «ГГГГ-ММ-ДД» по UTC
    user_id = Column(Integer, primary_key=True)
    count = Column(Integer, default=0)
