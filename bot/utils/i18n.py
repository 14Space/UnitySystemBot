"""
Мультиязычность бота: все надписи на ru / uk / en в одном месте.

Как пользоваться:
    from bot.utils.i18n import t, lang_of
    lang = lang_of(message.from_user)        # 'ru' | 'uk' | 'en'
    await message.answer(t("unsupported_link", lang))
    await message.answer(t("choose_quality", lang, title="Видео"))  # с подстановкой

Язык берётся из настроек Telegram пользователя (language_code). Русский и
украинский отдаём как есть, всё остальное – на английском (для иностранцев).
"""

SUPPORTED = ("ru", "uk", "en")
DEFAULT_LANG = "en"  # для всех, у кого Telegram не на русском/украинском


def lang_of(user) -> str:
    """Определяет язык пользователя по настройкам Telegram."""
    code = (getattr(user, "language_code", None) or "")[:2].lower()
    return code if code in SUPPORTED else DEFAULT_LANG


def t(key: str, lang: str, **kwargs) -> str:
    """Возвращает надпись по ключу на нужном языке (с подстановкой {переменных})."""
    entry = TEXTS.get(key, {})
    text = entry.get(lang) or entry.get("ru") or key
    return text.format(**kwargs) if kwargs else text


def t_kind(kind: str, lang: str) -> str:
    """Переводит «вид коллекции» (Альбом/Плейлист/Сет), пришедший из worker'а."""
    return _KINDS.get(kind, {}).get(lang, kind)


_KINDS = {
    "Альбом": {"ru": "Альбом", "uk": "Альбом", "en": "Album"},
    "Плейлист": {"ru": "Плейлист", "uk": "Плейлист", "en": "Playlist"},
    "Сет": {"ru": "Сет", "uk": "Сет", "en": "Set"},
}


TEXTS = {
    # --- Приветствие /start ---
    "welcome": {
        "ru": (
            "Привет! \n"
            "Отправь ссылку – скачаю видео, фото или музыку из YouTube, TikTok, Instagram, "
            "Pinterest, Spotify, SoundCloud, PornHub, HDRezka и других площадок 🙃\n\n"
            "Что умеет viaSaver: \n"
            "🎬 Выбор качества видео;\n"
            "🎵 Скачивание музыки с тегами;\n"
            "🌐 Загрузка с нестандартных ресурсов;\n"
            "📸 Скачивание фото и каруселей целиком;\n"
            "⚡️ Reels, Shorts и клипы – мгновенная загрузка в максимальном качестве.\n\n"
            "По вопросам: @viaWarrior\n"
            "Наш канал: @UnitySystem"
        ),
        "uk": (
            "Привіт!\n"
            "Надішли посилання – завантажу відео, фото або музику з YouTube, TikTok, Instagram, "
            "Pinterest, Spotify, SoundCloud, PornHub, HDRezka та інших платформ 🙃\n\n"
            "Що вміє viaSaver:\n"
            "🎬 Вибір якості відео;\n"
            "🎵 Завантаження музики з тегами;\n"
            "🌐 Завантаження з нестандартних ресурсів;\n"
            "📸 Завантаження фото та каруселей повністю;\n"
            "⚡️ Reels, Shorts та кліпи – миттєве завантаження в максимальній якості.\n\n"
            "З питань: @viaWarrior\n"
            "Канал: @UnitySystem"
        ),
        "en": (
            "Hi!\n"
            "Send a link – I will download video, photo, or music from YouTube, TikTok, Instagram, "
            "Pinterest, Spotify, SoundCloud, PornHub, HDRezka & more 🙃\n\n"
            "What viaSaver can do:\n"
            "🎬 Video quality selection;\n"
            "🎵 Downloading music with tags;\n"
            "🌐 Downloading from non-standard resources;\n"
            "📸 Downloading photos and full carousels;\n"
            "⚡️ Reels, Shorts, and clips – instant download in maximum quality.\n\n"
            "Questions: @viaWarrior\n"
            "Channel: @UnitySystem"
        ),
    },

    # --- Справка /help ---
    "help": {
        "ru": (
            "ℹ️ <b>Как пользоваться viaSaver</b>\n\n"
            "Просто пришли ссылку — я скачаю видео, фото или музыку из YouTube, TikTok, "
            "Instagram, Pinterest, Spotify, SoundCloud, PornHub или HDRezka.\n"
            "А ещё пришли голосовое или видео-кружок — расшифрую его в текст.\n\n"
            "<b>Команды:</b>\n"
            "/start — перезапустить бота\n"
            "/help — эта справка\n"
            "/premium — оформить Premium ✨"
        ),
        "uk": (
            "ℹ️ <b>Як користуватися viaSaver</b>\n\n"
            "Просто надішли посилання — я завантажу відео, фото або музику з YouTube, TikTok, "
            "Instagram, Pinterest, Spotify, SoundCloud, PornHub чи HDRezka.\n"
            "А ще надішли голосове або відео-кружечок — розшифрую його в текст.\n\n"
            "<b>Команди:</b>\n"
            "/start — перезапустити бота\n"
            "/help — ця довідка\n"
            "/premium — оформити Premium ✨"
        ),
        "en": (
            "ℹ️ <b>How to use viaSaver</b>\n\n"
            "Just send a link — I'll download video, photo, or music from YouTube, TikTok, "
            "Instagram, Pinterest, Spotify, SoundCloud, PornHub, or HDRezka.\n"
            "You can also send a voice message or video note — I'll transcribe it to text.\n\n"
            "<b>Commands:</b>\n"
            "/start — restart the bot\n"
            "/help — this help\n"
            "/premium — get Premium ✨"
        ),
    },
    "help_admin_extra": {
        "ru": "\n\n<b>Админ:</b>\n/statistics — статистика\n/cleancache — очистить кэш",
        "uk": "\n\n<b>Адмін:</b>\n/statistics — статистика\n/cleancache — очистити кеш",
        "en": "\n\n<b>Admin:</b>\n/statistics — statistics\n/cleancache — clear cache",
    },
    "cache_cleared": {
        "ru": "🧹 Кэш очищен: удалено записей — <b>{count}</b>.\nФайлы в Telegram не тронуты, бот просто перекачает их заново при следующем запросе.",
        "uk": "🧹 Кеш очищено: видалено записів — <b>{count}</b>.\nФайли в Telegram не зачеплені, бот просто перезавантажить їх при наступному запиті.",
        "en": "🧹 Cache cleared: <b>{count}</b> entries removed.\nTelegram files are untouched; the bot will just re-download them on next request.",
    },

    # --- Общие ---
    "unsupported_link": {
        "ru": "Эта ссылка пока не поддерживается.",
        "uk": "Це посилання поки не підтримується.",
        "en": "This link is not supported yet.",
    },
    "wait_current": {
        "ru": "⏳ Сначала дождись текущей загрузки.",
        "uk": "⏳ Спочатку дочекайся поточного завантаження.",
        "en": "⏳ Wait for the current download to finish first.",
    },
    "too_many_requests": {
        "ru": "⏳ Слишком много запросов, подожди немного.",
        "uk": "⏳ Забагато запитів, зачекай трохи.",
        "en": "⏳ Too many requests, please wait a moment.",
    },
    "link_expired": {
        "ru": "Ссылка устарела, отправь её ещё раз",
        "uk": "Посилання застаріло, надішли його ще раз",
        "en": "This link has expired, send it again",
    },
    "downloading": {
        "ru": "⏳ Скачиваю...",
        "uk": "⏳ Завантажую...",
        "en": "⏳ Downloading...",
    },
    "uploading": {
        "ru": "📤 Отправляю...",
        "uk": "📤 Надсилаю...",
        "en": "📤 Uploading...",
    },
    "processing": {
        "ru": "⚙️ Обрабатываю...",
        "uk": "⚙️ Обробляю...",
        "en": "⚙️ Processing...",
    },
    "premium_alert": {
        "ru": "Premium ✨",
        "uk": "Premium ✨",
        "en": "Premium ✨",
    },
    "feature_wip": {
        "ru": "Эта функция в разработке, скоро будет доступна 🚀",
        "uk": "Ця функція в розробці, скоро буде доступна 🚀",
        "en": "This feature is in development, coming soon 🚀",
    },

    # --- YouTube / PornHub (видео с выбором качества) ---
    "searching_video": {
        "ru": "⏳ Ищу видосик...",
        "uk": "⏳ Шукаю відео...",
        "en": "⏳ Looking for the video...",
    },
    "choose_quality": {
        "ru": "{title}\n\nВыбери качество:",
        "uk": "{title}\n\nОбери якість:",
        "en": "{title}\n\nChoose quality:",
    },
    "video_info_failed": {
        "ru": "Не удалось получить информацию о видео.\nПроверь ссылку или попробуй ещё раз.",
        "uk": "Не вдалося отримати інформацію про відео.\nПеревір посилання або спробуй ще раз.",
        "en": "Couldn't get video info.\nCheck the link or try again.",
    },

    # --- HDRezka ---
    "searching_movie": {
        "ru": "⏳ Ищу фильм/сериал...",
        "uk": "⏳ Шукаю фільм/серіал...",
        "en": "⏳ Looking for the movie/series...",
    },
    "hdrezka_open_failed": {
        "ru": "Не удалось открыть страницу HDRezka, проверь ссылку.",
        "uk": "Не вдалося відкрити сторінку HDRezka, перевір посилання.",
        "en": "Couldn't open the HDRezka page, check the link.",
    },
    "word_series": {"ru": "Сериал", "uk": "Серіал", "en": "Series"},
    "word_movie": {"ru": "Фильм", "uk": "Фільм", "en": "Movie"},
    "word_season": {"ru": "Сезон", "uk": "Сезон", "en": "Season"},
    "word_episode": {"ru": "серия", "uk": "серія", "en": "episode"},
    "label_choose_season": {
        "ru": "Выбери сезон:", "uk": "Обери сезон:", "en": "Choose a season:",
    },
    "label_choose_episode": {
        "ru": "Выбери серию:", "uk": "Обери серію:", "en": "Choose an episode:",
    },
    "label_choose_translation": {
        "ru": "Выбери озвучку:", "uk": "Обери озвучення:", "en": "Choose audio track:",
    },
    "label_choose_quality": {
        "ru": "Выбери качество:", "uk": "Обери якість:", "en": "Choose quality:",
    },
    "label_translation": {
        "ru": "Озвучка: {name}", "uk": "Озвучення: {name}", "en": "Audio: {name}",
    },
    "getting_qualities": {
        "ru": "⏳ Получаю качества...",
        "uk": "⏳ Отримую якості...",
        "en": "⏳ Getting available qualities...",
    },
    "translation_failed": {
        "ru": "Не удалось получить данные этой озвучки.",
        "uk": "Не вдалося отримати дані цього озвучення.",
        "en": "Couldn't get data for this audio track.",
    },
    "reselect_translation": {
        "ru": "Выбери озвучку заново",
        "uk": "Обери озвучення заново",
        "en": "Select the audio track again",
    },
    "btn_back_to_seasons": {
        "ru": "← К сезонам", "uk": "← До сезонів", "en": "← To seasons",
    },
    "btn_season": {
        "ru": "Сезон {n}", "uk": "Сезон {n}", "en": "Season {n}",
    },
    "nav_back": {"ru": "← Назад", "uk": "← Назад", "en": "← Back"},
    "nav_forward": {"ru": "Вперёд →", "uk": "Вперед →", "en": "Next →"},

    # --- Фото/видео-наборы ---
    "no_media": {
        "ru": "Тут нет медиа для скачивания.",
        "uk": "Тут немає медіа для завантаження.",
        "en": "There's no media to download here.",
    },
    "tt_slideshow_ask": {
        "ru": "Это слайдшоу, как его скачать?",
        "uk": "Це слайдшоу, як його завантажити?",
        "en": "This is a slideshow, how to download it?",
    },
    "tt_as_video": {
        "ru": "Видео",
        "uk": "Відео",
        "en": "Video",
    },
    "tt_as_photos": {
        "ru": "Фото",
        "uk": "Фото",
        "en": "Photos",
    },
    "ig_post_failed": {
        "ru": "Не удалось скачать пост, возможно, он приватный.",
        "uk": "Не вдалося завантажити пост, можливо, він приватний.",
        "en": "Couldn't download the post, it might be private.",
    },
    "generic_dl_failed": {
        "ru": "Не удалось скачать, проверь ссылку или попробуй позже.",
        "uk": "Не вдалося завантажити, перевір посилання або спробуй пізніше.",
        "en": "Download failed, check the link or try again later.",
    },

    # --- Аудио / коллекции ---
    "track_read_failed": {
        "ru": "Не удалось прочитать трек, проверь ссылку или попробуй позже.",
        "uk": "Не вдалося прочитати трек, перевір посилання або спробуй пізніше.",
        "en": "Couldn't read the track, check the link or try again later.",
    },
    "spotify_no_playlist": {
        "ru": "Spotify не отдаёт этот плейлист...",
        "uk": "Spotify не віддає цей плейлист...",
        "en": "Spotify won't share this playlist...",
    },
    "collection_read_failed": {
        "ru": "Не удалось прочитать альбом/плейлист, проверь ссылку или попробуй позже.",
        "uk": "Не вдалося прочитати альбом/плейлист, перевір посилання або спробуй пізніше.",
        "en": "Couldn't read the album/playlist, check the link or try again later.",
    },
    "empty_album": {
        "ru": "В этом альбоме нет треков для скачивания.",
        "uk": "У цьому альбомі немає треків для завантаження.",
        "en": "This album has no tracks to download.",
    },
    "soundcloud_set_failed": {
        "ru": "Не удалось прочитать альбом/плейлист SoundCloud. Проверь ссылку.",
        "uk": "Не вдалося прочитати альбом/плейлист SoundCloud. Перевір посилання.",
        "en": "Couldn't read the SoundCloud album/playlist. Check the link.",
    },
    "collection_caption": {
        "ru": "{kind}: {title}\n{count} {tracks_word}{extra}\n\nВыбери трек:",
        "uk": "{kind}: {title}\n{count} {tracks_word}{extra}\n\nОбери трек:",
        "en": "{kind}: {title}\n{count} {tracks_word}{extra}\n\nChoose a track:",
    },
    "tracks_word": {"ru": "треков", "uk": "треків", "en": "tracks"},
    "minutes_suffix": {
        "ru": ", ~{min} мин", "uk": ", ~{min} хв", "en": ", ~{min} min",
    },
    "btn_download_all": {
        "ru": "Скачать всё", "uk": "Завантажити все", "en": "Download all",
    },
    "downloading_all": {
        "ru": "⬇️ Скачиваю весь список: {i} / {total}",
        "uk": "⬇️ Завантажую весь список: {i} / {total}",
        "en": "⬇️ Downloading the whole list: {i} / {total}",
    },

    # --- Расшифровка ГС/кружков ---
    "transcribing": {
        "ru": "Расшифровываю...",
        "uk": "Розшифровую...",
        "en": "Transcribing...",
    },
    "transcribe_nothing": {
        "ru": "Не удалось ничего распознать...",
        "uk": "Не вдалося нічого розпізнати...",
        "en": "Couldn't recognize anything...",
    },

    # --- Premium / оплата ---
    "premium_desc": {
        "ru": "С Premium ⭐ видео можно скачивать в самом высоком разрешении, а музыку – целыми альбомами.",
        "uk": "З Premium ⭐ відео можна завантажувати в найвищій якості, а музику – цілими альбомами.",
        "en": "With Premium ⭐ you can download videos in the highest resolution and music as full albums.",
    },
    "premium_text": {
        "ru": "✨ <b>viaSaver Premium</b>\n\n{desc}\n\nРазовая покупка, навсегда. Цена: <b>{price} ⭐</b>",
        "uk": "✨ <b>viaSaver Premium</b>\n\n{desc}\n\nРазова покупка, назавжди. Ціна: <b>{price} ⭐</b>",
        "en": "✨ <b>viaSaver Premium</b>\n\n{desc}\n\nOne-time purchase, forever. Price: <b>{price} ⭐</b>",
    },
    "btn_buy": {
        "ru": "Купить за {price} ⭐", "uk": "Купити за {price} ⭐", "en": "Buy for {price} ⭐",
    },
    "already_premium": {
        "ru": "✨ У тебя уже есть Premium – все функции открыты!",
        "uk": "✨ У тебе вже є Premium – усі функції відкриті!",
        "en": "✨ You already have Premium – all features unlocked!",
    },
    "already_premium_short": {
        "ru": "✨ У тебя уже есть Premium!",
        "uk": "✨ У тебе вже є Premium!",
        "en": "✨ You already have Premium!",
    },
    "payment_success": {
        "ru": "✨ Спасибо за покупку! Premium активирован 🎉\nВсе функции открыты.",
        "uk": "✨ Дякуємо за покупку! Premium активовано 🎉\nУсі функції відкриті.",
        "en": "✨ Thanks for your purchase! Premium activated 🎉\nAll features unlocked.",
    },

    # --- Inline-режим ---
    "inline_cached_video_title": {
        "ru": "Скачать видео", "uk": "Завантажити відео", "en": "Download video",
    },
    "inline_article_title": {
        "ru": "Скачать через viaSaver",
        "uk": "Завантажити через viaSaver",
        "en": "Download via viaSaver",
    },
    "inline_article_desc": {
        "ru": "Нажми кнопку – бот скачает в личке",
        "uk": "Натисни кнопку – бот завантажить у приваті",
        "en": "Tap the button – the bot will download in DM",
    },
    "inline_message_text": {
        "ru": "📥 Скачать: {url}", "uk": "📥 Завантажити: {url}", "en": "📥 Download: {url}",
    },
    "btn_download_in_bot": {
        "ru": "📥 Скачать в боте",
        "uk": "📥 Завантажити в боті",
        "en": "📥 Download in the bot",
    },

    # --- Ошибки (friendly_error) ---
    "err_too_large": {
        "ru": "🔒 Файл слишком большой (больше 2 ГБ). Выбери качество пониже.",
        "uk": "🔒 Файл занадто великий (більше 2 ГБ). Обери якість нижче.",
        "en": "🔒 The file is too large (over 2 GB). Pick a lower quality.",
    },
    "err_drm": {
        "ru": "🔒 Этот трек защищён DRM и недоступен для скачивания.",
        "uk": "🔒 Цей трек захищений DRM і недоступний для завантаження.",
        "en": "🔒 This track is DRM-protected and can't be downloaded.",
    },
    "err_restricted": {
        "ru": "🔒 Контент ограничен (18+ или закрытая аудитория) – скачать нельзя.",
        "uk": "🔒 Контент обмежений (18+ або закрита аудиторія) – завантажити не можна.",
        "en": "🔒 Content is restricted (18+ or limited audience) – can't download.",
    },
    "err_login_required": {
        "ru": "🔒 Это видео доступно только пользователям, вошедшим в аккаунт (ограниченная аудитория). Сейчас скачать не удалось — попробуй позже.",
        "uk": "🔒 Це відео доступне лише користувачам, які увійшли в акаунт (обмежена аудиторія). Зараз завантажити не вдалося — спробуй пізніше.",
        "en": "🔒 This video is only available to logged-in users (limited audience). Couldn't download it right now — try again later.",
    },
    "err_private": {
        "ru": "🔒 Контент приватный или требует входа в аккаунт.",
        "uk": "🔒 Контент приватний або потребує входу в акаунт.",
        "en": "🔒 Content is private or requires sign-in.",
    },
    "err_geo": {
        "ru": "🌍 Недоступно в этом регионе (гео-блокировка).",
        "uk": "🌍 Недоступно в цьому регіоні (гео-блокування).",
        "en": "🌍 Not available in this region (geo-block).",
    },
    "err_age": {
        "ru": "🔞 Контент с возрастным ограничением – скачать не удалось.",
        "uk": "🔞 Контент із віковим обмеженням – завантажити не вдалося.",
        "en": "🔞 Age-restricted content – download failed.",
    },
    "err_not_found": {
        "ru": "❌ Контент не найден или был удалён.",
        "uk": "❌ Контент не знайдено або його було видалено.",
        "en": "❌ Content not found or was removed.",
    },
    "err_network": {
        "ru": "🌐 Проблема с сетью. Попробуй ещё раз чуть позже.",
        "uk": "🌐 Проблема з мережею. Спробуй ще раз трохи пізніше.",
        "en": "🌐 Network problem. Try again a bit later.",
    },
}
