from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

router = Router()

MESSAGES = {
    "ru": (
        "Привет! \n"
        "Отправь ссылку – скачаю видео, фото или музыку из YouTube, TikTok, Instagram, "
        "Pinterest, Spotify, SoundCloud, PornHub или HDRezka 🙃\n\n"
        "Что умеет viaSaver: \n"
        "🎬 Выбор качества видео;\n"
        "🎵 Скачивание музыки с тегами;\n"
        "🌐 Загрузка с нестандартных ресурсов;\n"
        "📸 Скачивание фото и каруселей целиком;\n"
        "⚡️ Reels, Shorts и клипы – мгновенная загрузка в максимальном качестве."
    ),
    "uk": (
        "Привіт!\n"
        "Надішли посилання – скачаю відео, фото або музику з YouTube, TikTok, Instagram, "
        "Pinterest, Spotify, SoundCloud, PornHub чи HDRezka 🙃\n\n"
        "Що вміє viaSaver:\n"
        "🎬 Вибір якості відео;\n"
        "🎵 Скачування музики з тегами;\n"
        "🌐 Завантаження з нестандартних ресурсів;\n"
        "📸 Скачування фото та каруселей повністю;\n"
        "⚡️ Reels, Shorts та кліпи – миттєве завантаження в максимальній якості."
    ),
    "en": (
        "Hi!\n"
        "Send a link – I will download video, photo, or music from YouTube, TikTok, Instagram, "
        "Pinterest, Spotify, SoundCloud, PornHub, or HDRezka 🙃\n\n"
        "What viaSaver can do:\n"
        "🎬 Video quality selection;\n"
        "🎵 Downloading music with tags;\n"
        "📸 Downloading photos and full carousels;\n"
        "🌐 Downloading from non-standard resources;\n"
        "⚡️ Reels, Shorts, and clips – instant download in maximum quality."
    ),
}


@router.message(CommandStart())
async def cmd_start(message: Message):
    lang = (message.from_user.language_code or "ru")[:2]
    text = MESSAGES.get(lang, MESSAGES["ru"])
    await message.answer(text)
