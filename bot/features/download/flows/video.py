"""
Видео: выбор качества (YouTube, PornHub), короткие ролики сразу (Shorts, Reels,
PornHub Shorties), одно медиа (Pinterest) и посты Instagram.
"""

import asyncio
import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from bot.features.download.keyboards.quality import build_quality_keyboard
from bot.config import DOWNLOADS_DIR, FREE_QUALITY_LIMIT
from bot.utils import limits, tg_files, chat_action
from bot.utils.tg_messages import safe_edit, safe_delete
from bot.features.common import alerts
from bot.utils.i18n import t, lang_of
from bot.database import SessionLocal
from bot.database.repository import is_premium
from bot.features.download import job
from bot.features.download.job import Cache
from bot.features.download.screens import Screens
from bot.features.download.downloaders.ytdlp_wrapper import (
    get_video_info, get_available_qualities, download_video, estimate_size, download_media,
    convert_gif_to_mp4,
)
from bot.features.download.downloaders.instagram import is_image
from bot.features.download.flows.common import (
    _alert,
    _heavy_from_cache,
    _nice_name,
    _pick_thumbnail,
    _reply_error,
    _send_cached_post,
    _send_media_files,
    _video_kwargs,
)

router = Router()
logger = logging.getLogger(__name__)


# Выбор качества: метаданные площадки живут только в памяти. После перезапуска
# кнопка поднимается из базы без них – качество скачается на полторы секунды дольше.
QUALITY = Screens("quality")


async def _handle_quality_video(message: Message, url: str, lang: str):
    """Получает метаданные и показывает выбор качества (YouTube, PornHub)."""
    status = await message.reply(t("searching_video", lang))
    try:
        info = await asyncio.to_thread(get_video_info, url)
        # Идущий (или предстоящий) прямой эфир не качаем — yt-dlp запишет лишь кусок
        # с момента подключения. Просим прислать ссылку после завершения трансляции.
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            await safe_edit(status, t("live_stream", lang))
            return
        available = await asyncio.to_thread(get_available_qualities, info)

        # «Мягкая» деградация источника: ответ пришёл, но данные неполные — исключения
        # нет, поэтому раньше такой сбой не попадал НИ в логи, НИ в алерты (слепое пятно).
        # Пустой список качеств = кнопки не покажутся, пользователь упрётся в тупик
        # «Выбери качество» без кнопок. Ловим явно: лог + тревога админу + честная ошибка.
        if not available:
            logger.warning("Пустой список качеств (деградация источника): %s", url)
            alerts.note_failure(RuntimeError(f"нет доступных качеств: {url}"))
            await safe_edit(status, t("video_info_failed", lang))
            return

        title = info.get("title")
        if not title:                     # заголовок не извлёкся — тоже сигнал деградации
            logger.warning("Метаданные без заголовка (деградация источника): %s", url)
            title = "Без названия"
        thumbnail = _pick_thumbnail(info)

        async with SessionLocal() as session:
            premium = await is_premium(session, message.from_user.id)

        await status.delete()
        # Метаданные — только в память: не запрашиваем источник второй раз. В базу уходит
        # ссылка и чей это запрос: кнопки в Telegram живут дольше, чем память бота.
        url_id = await QUALITY.open(message, url, premium=premium, memory={"info": info})

        caption = t("choose_quality", lang, title=title)

        keyboard = build_quality_keyboard(url_id, available, premium)

        # Превью — украшение, кнопки — суть. Telegram иногда отказывается забирать
        # картинку по ссылке («wrong type of the web page content») даже когда она живая
        # и обычного размера. Без запасного пути падал весь экран выбора, и человек
        # вместо кнопок получал ошибку. Теперь в худшем случае будет текст с кнопками.
        sent = False
        if thumbnail:
            try:
                await message.answer_photo(thumbnail, caption=caption, reply_markup=keyboard)
                sent = True
            except Exception as e:
                logger.info("Превью не отправилось (%s) — показываю выбор качества текстом",
                            str(e)[:80])
        if not sent:
            await message.answer(caption, reply_markup=keyboard)

    except Exception as e:
        logger.exception("Failed to get video info")
        alerts.note_failure(e)            # раньше этот путь молчал в алертах — теперь нет
        await safe_edit(status, t("video_info_failed", lang))


async def _handle_simple_video(message: Message, url: str, download_fn, cache_key: str, lang: str,
                               use_title: bool = True):
    """Качает короткое видео сразу (Shorts, Instagram Reel): кэш, лимит, отправка.
    Одну и ту же ссылку качает только один запрос — остальные ждут и берут из кэша
    (см. bot.utils.inflight)."""
    async def send(file_id):
        await message.reply_video(file_id, supports_streaming=True)

    async def work(paths):
        # «Отправляет видео…» в шапке чата, пока качаем и заливаем: иначе
        # непонятно, живой бот или задумался (см. bot/utils/chat_action.py).
        async with chat_action.show(message.bot, message.chat.id, chat_action.VIDEO):
            file_path = paths.keep(await asyncio.to_thread(download_fn, url))
            sent = await message.reply_video(
                await tg_files.input_file_async(
                    file_path, await _nice_name(file_path, use_title=use_title)),
                **await _video_kwargs(file_path))
        return sent.video.file_id if sent.video else None

    # Лёгкие задачи не ограничиваем «одна за раз» — можно кидать подряд, общий
    # лимит (limits.LIGHT) сам поставит лишние в очередь.
    await job.run(cache=Cache(url, cache_key), send=send, work=work,
                  on_error=_reply_error(message, lang), tell=message, lang=lang,
                  label=cache_key, dedupe=True)


async def _handle_media(message: Message, url: str, cache_key: str, lang: str):
    """Качает одно медиа (Pinterest) и шлёт как фото/гиф/видео — по типу файла.
    Дедуп по ссылке: параллельные запросы одной ссылки не качают повторно (inflight)."""
    # Кэш: в file_id храним префикс типа — "P:" фото, "A:" гиф, "V:" видео
    async def send(cached):
        if cached.startswith("P:"):
            await message.reply_photo(cached[2:])
        elif cached.startswith("A:"):
            await message.reply_animation(cached[2:])
        else:
            await message.reply_video(cached[2:], supports_streaming=True)

    async def work(paths):
        file_path = paths.keep(await asyncio.to_thread(download_media, url))
        if file_path.lower().endswith(".gif"):
            # GIF → чистый mp4 (без грубой авто-конвертации Telegram), шлём анимацией
            mp4 = paths.keep(await asyncio.to_thread(convert_gif_to_mp4, file_path))
            sent = await message.reply_animation(await tg_files.input_file_async(
                mp4, await _nice_name(mp4, quality="", use_title=False)))
            return "A:" + sent.animation.file_id if sent.animation else None
        if is_image(file_path):
            sent = await message.reply_photo(await tg_files.input_file_async(
                file_path, await _nice_name(file_path, quality="", use_title=False)))
            return "P:" + sent.photo[-1].file_id if sent.photo else None
        sent = await message.reply_video(
            await tg_files.input_file_async(file_path, await _nice_name(file_path, use_title=False)),
            **await _video_kwargs(file_path))
        return "V:" + sent.video.file_id if sent.video else None

    await job.run(cache=Cache(url, cache_key), send=send, work=work,
                  on_error=_reply_error(message, lang), tell=message, lang=lang,
                  label=cache_key, dedupe=True)


async def _handle_files(message: Message, url: str, download_fn, error_key: str, lang: str):
    """Качает набор файлов (Instagram пост) и отдаёт фото/видео или альбомом.
    Дедуп по ссылке: параллельные запросы одной ссылки не качают повторно (inflight)."""
    async def work(paths):
        # «Отправляет фото…» в шапке чата, пока качаем и заливаем альбом.
        async with chat_action.show(message.bot, message.chat.id, chat_action.PHOTO):
            files = paths.keep(await asyncio.to_thread(download_fn, url))
        if not files:
            # Пустой результат у поста/карусели = обычно деградация источника, а не
            # реально пустой пост. Раньше молчали — теперь видно в логах и в алерте.
            logger.warning("%s: пустой результат (нет медиа) — %s", error_key, url)
            alerts.note_failure(RuntimeError(f"пустой результат ({error_key}): {url}"))
            await message.reply(t("no_media", lang))
            return None
        tokens = await _send_media_files(message, files, lang)
        return "\n".join(tokens) or None

    await job.run(cache=Cache(url, "post"), send=lambda v: _send_cached_post(message, v),
                  work=work, on_error=_reply_error(message, lang, error_key),
                  tell=message, lang=lang, label=url, dedupe=True)


@router.callback_query(F.data.startswith("quality:"))
async def handle_quality_choice(callback: CallbackQuery, bot: Bot):
    lang = lang_of(callback.from_user)
    _, quality_str, url_id = callback.data.split(":", 2)
    if not quality_str.isdigit():
        await callback.answer(t("link_expired", lang), show_alert=True)
        return
    quality = int(quality_str)

    # В памяти нет — бот перезапускался: экран поднимется из базы, но без метаданных,
    # поэтому качество скачается на полторы секунды дольше, зато кнопка живая.
    entry = await QUALITY.for_click(callback, url_id, lang)
    if not entry:
        return

    chat_id = entry["chat_id"]
    user_msg_id = entry["user_msg_id"]
    url = entry["url"]
    info = entry.get("info")
    user_id = callback.from_user.id
    # Контекст для тревог о сбоях: этот сбой пришёл из колбэка (кнопки), а не из входящей
    # ссылки, поэтому ставим контекст здесь — иначе в алерте был бы прочерк «—».
    alerts.current_request.set(f"видео {quality}p: {url}")

    # Защита: качество выше 720p — только для Premium
    # Premium проверяем У ТОГО, КТО НАЖАЛ, а не у того, кто прислал ссылку. Раньше
    # смотрели на отметку в самом меню (entry["premium"]) — то есть в группе любой мог
    # открыть чужое меню Premium-пользователя и скачать 4K бесплатно.
    if quality > FREE_QUALITY_LIMIT:
        async with SessionLocal() as session:
            if not await is_premium(session, user_id):
                await callback.answer(t("premium_alert", lang), show_alert=True)
                return

    # Кэш: если это качество уже качали — отдаём мгновенно (блокировку не применяем)
    cache = Cache(url, str(quality))

    async def send_cached(file_id):
        await bot.send_video(chat_id, file_id, supports_streaming=True,
                             reply_to_message_id=user_msg_id)

    got = await _heavy_from_cache(callback, cache, send_cached)
    if got:
        return
    # Расписка оказалась мёртвой — качаем это качество заново, как в первый раз.
    answered = got is False

    # Размер известен заранее — не тратим полчаса и гигабайты домашнего канала на файл,
    # который Telegram всё равно не примет. Оценка приблизительная, поэтому берём запас:
    # предупреждаем только при явном превышении потолка.
    if info:
        approx = estimate_size(info, quality)
        if approx and approx > limits.MAX_FILE_BYTES:
            await _alert(callback, answered, chat_id,
                         t("too_big_before", lang, size=f"{approx / 1024 / 1024 / 1024:.1f}"))
            return

    # Один пользователь — одна активная загрузка. Меню не удаляем, чтобы можно было повторить.
    # Проверка и отметка – один шаг (см. handle_hdrezka_quality).
    async with job.exclusive(user_id) as mine:
        if not mine:
            await _alert(callback, answered, chat_id, t("wait_current", lang))
            return
        if not answered:
            await callback.answer()
            await safe_delete(callback.message)
        progress = job.Progress(bot, chat_id, user_msg_id, lang)

        async def work(paths):
            await progress.start()
            duration = int(info.get("duration", 0) or 0) if info else 0
            # Место на диске проверяем ДО загрузки: на забитом диске yt-dlp и ffmpeg падают
            # с невнятным «errno 28», а человек видит бессмысленное «не удалось скачать».
            limits.check_disk_space(DOWNLOADS_DIR)
            # info уже получен, когда показывали кнопки качества — передаём его, чтобы
            # yt-dlp не ходил к площадке за теми же метаданными второй раз (экономит ~1.5с).
            # «Отправляет видео…» в шапке: полоска показывает скачивание, а заливка идёт
            # уже после неё, и без действия в чате выглядит как зависание.
            async with chat_action.show(bot, chat_id, chat_action.VIDEO):
                file_path = paths.keep(await asyncio.to_thread(
                    download_video, url, quality, progress.hook, progress.stage("processing"), info))
                # Не удаляем статус, а показываем «Отправляю» — заливка тоже занимает время
                await progress.say("uploading")
                sent = await bot.send_video(
                    chat_id,
                    await tg_files.input_file_async(
                        file_path,
                        await _nice_name(file_path,
                                         quality=f"{quality}p" if quality else None)),
                    reply_to_message_id=user_msg_id,
                    **await _video_kwargs(file_path, duration),
                )
            await progress.done()
            return sent.video.file_id if sent.video else None

        async def on_error(e):
            await progress.fail(limits.friendly_error(e, lang))

        await job.produce(cache=cache, work=work, on_error=on_error, lane=limits.HEAVY,
                          label=f"видео {quality}p {url}")
