"""
HDRezka: фильм → озвучка → качество; сериал → сезон → серия → озвучка → качество.
Экран держит живую сессию с сайтом; после перезапуска бота она открывается заново.
"""

import asyncio
import logging
import os
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from bot.config import DOWNLOADS_DIR, FREE_QUALITY_LIMIT
from bot.utils import limits, tg_files
from bot.utils import home_tunnel  # ВРЕМЕННО home_tunnel
from bot.utils.tg_messages import safe_edit, safe_delete
from bot.features.common import alerts
from bot.utils.i18n import t, lang_of
from bot.database import SessionLocal
from bot.database.repository import is_premium
from bot.features.download import job
from bot.features.download.job import Cache
from bot.features.download.screens import Screens
from bot.features.download.downloaders import hdrezka
from bot.features.download.keyboards.hdrezka import (
    build_translator_keyboard, build_hdrezka_quality_keyboard, build_season_keyboard,
    build_episode_keyboard,
)
from bot.features.download.flows.common import _alert, _heavy_from_cache, _video_kwargs

router = Router()
logger = logging.getLogger(__name__)


# --- Экраны с кнопками (см. screens.py) -------------------------------------

def _hdrezka_state(api, info: dict) -> dict:
    """Что помнит экран HDRezka, кроме общих полей: объект сессии (переиспользуем на
    всех шагах, чтобы не качать страницу заново), разбор страницы и выбор человека."""
    return {
        "api": api,
        "name": info["name"],
        "is_series": info["is_series"],
        "translators": info.get("translators", []),
        "seasons": info.get("seasons", []),
        "season": None,
        "episode": None,
        "thumbnail": info.get("thumbnail"),
        "streams": {},  # tid -> объект потока (кэш между «озвучкой» и «качеством»)
    }


async def _restore_hdrezka(saved: dict) -> dict | None:
    """Экран HDRezka после перезапуска бота: страницу и объект сессии сохранить нельзя,
    но их можно открыть повторно. Это стоит секунд (анти-бот-проверка и чтение
    страницы), зато кнопка работает, а не отвечает «ссылка устарела».

    Сезон и серию не восстанавливаем: человек выберет их теми же кнопками. Потоки
    (streams) тоже – они добываются при выборе озвучки и живут недолго.
    """
    try:
        api = await asyncio.to_thread(hdrezka.open_media, saved["url"])
        info = await asyncio.to_thread(hdrezka.get_info, api, saved["url"])
    except Exception as e:
        logger.warning("HDRezka: не смог восстановить экран", exc_info=True)
        alerts.note_failure(e)
        return None
    return _hdrezka_state(api, info)


HDREZKA = Screens("hdrezka", _restore_hdrezka)


async def _handle_hdrezka(message: Message, url: str, lang: str):
    """HDRezka: фильм → озвучки; сериал → сначала сезон и серия."""
    status = await message.reply(t("searching_movie", lang))
    try:
        api = await asyncio.to_thread(hdrezka.open_media, url)
        # В потоке, как и открытие страницы: у сериала get_info делает ещё один
        # запрос к сайту (список сезонов) и умеет ждать между повторами. В главном
        # потоке это значило, что бот на пару секунд замирал для ВСЕХ, а не только
        # для того, кто прислал ссылку.
        info = await asyncio.to_thread(hdrezka.get_info, api, url)
    except Exception as e:
        logger.exception("HDRezka info failed")
        if await home_tunnel.check_now():   # ВРЕМЕННО home_tunnel: дом выключен – молчим
            await safe_delete(status)
            return
        alerts.note_failure(e)            # fetch-ошибки тоже должны доходить до админа
        await safe_edit(status, t("hdrezka_open_failed", lang))
        return

    async with SessionLocal() as session:
        premium = await is_premium(session, message.from_user.id)

    # В базу — ссылка и чей это запрос; объект сессии и разбор страницы — в память
    # (после перезапуска их откроет заново _restore_hdrezka).
    sid = await HDREZKA.open(message, url, premium=premium, memory=_hdrezka_state(api, info))
    await status.delete()

    if info["is_series"]:
        caption = f"{t('word_series', lang)}: {info['name']}\n\n{t('label_choose_season', lang)}"
        keyboard = build_season_keyboard(sid, info["seasons"], lang)
    else:
        caption = f"{t('word_movie', lang)}: {info['name']}\n\n{t('label_choose_translation', lang)}"
        keyboard = build_translator_keyboard(sid, info["translators"], 0, lang)

    if info.get("thumbnail"):
        await message.answer_photo(info["thumbnail"], caption=caption, reply_markup=keyboard)
    else:
        await message.answer(caption, reply_markup=keyboard)


@router.callback_query(F.data.startswith("hrss:"))
async def handle_hdrezka_season(callback: CallbackQuery):
    """Сезон выбран — показываем серии"""
    lang = lang_of(callback.from_user)
    _, sid, season = callback.data.split(":")
    entry = await HDREZKA.for_click(callback, sid, lang)
    if not entry:
        return
    season = int(season)
    entry["season"] = season
    await callback.answer()
    episodes = await asyncio.to_thread(hdrezka.get_episodes, entry["api"], season)
    text = (f"{t('word_series', lang)}: {entry['name']}\n{t('word_season', lang)} {season}"
            f"\n\n{t('label_choose_episode', lang)}")
    await _edit_or_caption(callback.message, text, build_episode_keyboard(sid, season, episodes, lang))


@router.callback_query(F.data.startswith("hrep:"))
async def handle_hdrezka_episode(callback: CallbackQuery):
    """Серия выбрана — показываем озвучки этой серии"""
    lang = lang_of(callback.from_user)
    _, sid, season, episode = callback.data.split(":")
    entry = await HDREZKA.for_click(callback, sid, lang)
    if not entry:
        return
    entry["season"] = int(season)
    entry["episode"] = int(episode)
    entry["streams"] = {}  # сменилась серия — старые потоки не подходят
    await callback.answer()
    translators = await asyncio.to_thread(
        hdrezka.get_translators, entry["api"], entry["season"], entry["episode"]
    )
    entry["translators"] = translators
    text = f"{_hdrezka_head(entry, lang)}\n\n{t('label_choose_translation', lang)}"
    await _edit_or_caption(callback.message, text, build_translator_keyboard(sid, translators, 0, lang))


@router.callback_query(F.data.startswith("hrback:"))
async def handle_hdrezka_back(callback: CallbackQuery):
    """Назад к выбору сезона"""
    lang = lang_of(callback.from_user)
    _, sid = callback.data.split(":")
    entry = await HDREZKA.for_click(callback, sid, lang)
    if not entry:
        return
    await callback.answer()
    text = f"{t('word_series', lang)}: {entry['name']}\n\n{t('label_choose_season', lang)}"
    await _edit_or_caption(callback.message, text, build_season_keyboard(sid, entry["seasons"], lang))


@router.callback_query(F.data.startswith("hrp:"))
async def handle_hdrezka_page(callback: CallbackQuery):
    """Пагинация списка озвучек"""
    lang = lang_of(callback.from_user)
    _, sid, page = callback.data.split(":")
    entry = await HDREZKA.for_click(callback, sid, lang)
    if not entry:
        return
    keyboard = build_translator_keyboard(sid, entry["translators"], int(page), lang)
    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


def _hdrezka_head(entry: dict, lang: str) -> str:
    """Шапка сообщения: «Фильм/Сериал: Название (+ сезон/серия)»."""
    kind = t("word_series", lang) if entry["is_series"] else t("word_movie", lang)
    head = f"{kind}: {entry['name']}"
    if entry["is_series"]:
        head += f"\n{t('word_season', lang)} {entry['season']}, {t('word_episode', lang)} {entry['episode']}"
    return head


async def _edit_or_caption(message, text: str, keyboard):
    """Редактирует сообщение: caption если есть обложка, иначе обычный текст."""
    try:
        await message.edit_caption(caption=text, reply_markup=keyboard)
    except Exception:
        try:
            await message.edit_text(text, reply_markup=keyboard)
        except Exception:
            pass


async def _show_hdrezka_quality(message, entry, sid, tid, lang):
    """Показывает кнопки качества для выбранной озвучки."""
    tname = dict(entry["translators"]).get(tid, "")
    qualities = hdrezka.stream_qualities(entry["streams"][tid])
    if not qualities:                 # поток без качеств — раньше был молчаливый тупик
        logger.warning("HDRezka: пустой список качеств (деградация): %s", entry.get("name"))
        alerts.note_failure(RuntimeError(f"HDRezka без качеств: {entry.get('name')}"))
        await _edit_or_caption(message, t("translation_failed", lang), None)
        return
    keyboard = build_hdrezka_quality_keyboard(sid, tid, qualities, entry.get("premium", False))
    text = (f"{_hdrezka_head(entry, lang)}\n{t('label_translation', lang, name=tname)}"
            f"\n\n{t('label_choose_quality', lang)}")
    await _edit_or_caption(message, text, keyboard)


@router.callback_query(F.data.startswith("hrt:"))
async def handle_hdrezka_translator(callback: CallbackQuery):
    """Озвучка выбрана — получаем поток (один запрос) и показываем качества"""
    lang = lang_of(callback.from_user)
    _, sid, tid = callback.data.split(":")
    entry = await HDREZKA.for_click(callback, sid, lang)
    if not entry:
        return

    alerts.current_request.set(f"hdrezka: {entry.get('name','')} — {entry.get('url','')}")
    tid = int(tid)
    await callback.answer()
    tname = dict(entry["translators"]).get(tid, "")
    # Сразу показываем «идёт загрузка», чтобы не выглядело зависшим
    await _edit_or_caption(
        callback.message,
        f"{_hdrezka_head(entry, lang)}\n{t('label_translation', lang, name=tname)}\n\n{t('getting_qualities', lang)}",
        None,
    )
    try:
        stream = await asyncio.to_thread(
            hdrezka.get_stream, entry["api"], tid, entry["season"], entry["episode"]
        )
    except Exception as e:
        logger.exception("HDRezka stream failed")
        alerts.note_failure(e)
        await _edit_or_caption(callback.message, t("translation_failed", lang), None)
        return

    entry["streams"][tid] = stream
    await _show_hdrezka_quality(callback.message, entry, sid, tid, lang)


@router.callback_query(F.data.startswith("hrq:"))
async def handle_hdrezka_quality(callback: CallbackQuery, bot: Bot):
    """Качество выбрано — качаем и отправляем"""
    lang = lang_of(callback.from_user)
    _, sid, tid, qidx = callback.data.split(":")
    entry = await HDREZKA.for_click(callback, sid, lang)
    if not entry:
        return

    alerts.current_request.set(f"hdrezka: {entry.get('name','')} — {entry.get('url','')}")
    tid = int(tid)
    stream = entry["streams"].get(tid)
    qualities = hdrezka.stream_qualities(stream) if stream else []
    if not stream or not qidx.isdigit() or int(qidx) >= len(qualities):
        await callback.answer(t("reselect_translation", lang), show_alert=True)
        return
    quality = qualities[int(qidx)]
    user_id = callback.from_user.id
    chat_id = entry["chat_id"]

    # Защита: качество выше 720p — только для Premium. Смотрим на нажавшего, а не на
    # отметку в экране: Premium мог закончиться с тех пор, как рисовали кнопки.
    height = 9999 if "K" in quality.upper() else int("".join(filter(str.isdigit, quality)) or 0)
    if height > FREE_QUALITY_LIMIT:
        async with SessionLocal() as session:
            if not await is_premium(session, user_id):
                await callback.answer(t("premium_alert", lang), show_alert=True)
                return

    # Кэш: этот фильм/серию в этой озвучке и качестве уже качали — отдаём мгновенно,
    # без повторного скачивания (ключ = ссылка + озвучка + сезон + серия, качество — отдельно).
    cache = Cache(f"{entry.get('url', '')}|hr|{tid}|{entry['season']}|{entry['episode']}",
                  quality)

    async def send_cached(file_id):
        await bot.send_video(chat_id, file_id, supports_streaming=True,
                             reply_to_message_id=entry["user_msg_id"])

    got = await _heavy_from_cache(callback, cache, send_cached)
    if got:
        return
    answered = got is False

    # Проверка «уже качает» и отметка – один шаг внутри exclusive: раньше между ними
    # стояли ответ на нажатие и удаление меню, и два быстрых нажатия на разные
    # качества успевали пройти проверку оба.
    async with job.exclusive(user_id) as mine:
        if not mine:
            await _alert(callback, answered, chat_id, t("wait_current", lang))
            return
        if not answered:
            await callback.answer()
            await safe_delete(callback.message)
        progress = job.Progress(bot, chat_id, entry["user_msg_id"], lang)

        async def work(paths):
            await progress.start()
            limits.check_disk_space(DOWNLOADS_DIR)      # фильм весит гигабайты
            file_path = paths.keep(await asyncio.to_thread(
                hdrezka.download_stream, stream, quality,
                entry["name"], entry["season"], entry["episode"], progress.hook,
            ))
            await progress.say("uploading")
            hr_title = entry["name"]
            if entry.get("season") and entry.get("episode"):
                hr_title = f"{hr_title} S{entry['season']:02d}E{entry['episode']:02d}"
            sent = await bot.send_video(
                chat_id,
                await tg_files.input_file_async(file_path, tg_files.display_name(
                    hr_title, str(quality or ""), os.path.splitext(file_path)[1] or ".mp4")),
                reply_to_message_id=entry["user_msg_id"],
                **await _video_kwargs(file_path),
            )
            await progress.done()
            return sent.video.file_id if sent.video else None

        async def on_error(e):
            await progress.fail(limits.friendly_error(e, lang))

        await job.produce(cache=cache, work=work, on_error=on_error, lane=limits.HEAVY,
                          label=f"HDRezka {quality}")
