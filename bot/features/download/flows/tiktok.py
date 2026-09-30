"""
TikTok: видео сразу, слайдшоу – по настройке чата (видео, фото или выбор кнопками),
плюс музыка к фото-слайдшоу.
"""

import asyncio
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from bot.utils.platform_detector import Platform
from bot.utils import limits, tg_files
from bot.utils import home_tunnel  # ВРЕМЕННО home_tunnel
from bot.utils.tg_messages import safe_edit, safe_delete
from bot.features.common import alerts
from bot.utils.i18n import t, lang_of
from bot.database import SessionLocal
from bot.database.repository import get_slideshow_mode, get_audio_track
from bot.features.download import job
from bot.features.download.job import Cache
from bot.features.download.screens import Screens
from bot.features.download.downloaders.audio_extract import extract_audio_track
from bot.features.download.downloaders import tiktok
from bot.features.download.keyboards.tiktok import build_tiktok_slideshow_keyboard
from bot.features.download.flows.common import (
    _claim,
    _nice_name,
    _quiet,
    _reply_error,
    _send_cached_post,
    _send_media_files,
    _shorts_cap,
    _unclaim,
)

router = Router()
logger = logging.getLogger(__name__)


async def _restore_tiktok(saved: dict) -> dict:
    """Экран «видео или фото» TikTok: данные поста (ссылки на кадры и звук) живут у
    TikTok недолго, поэтому мы их не храним, а перезапрашиваем – это один запрос."""
    info = await asyncio.to_thread(tiktok.fetch_tiktok, saved["url"])
    return {"info": info, "cache_url": saved.get("cache_url") or saved["url"]}


TIKTOK = Screens("tiktok", _restore_tiktok)


async def _maybe_send_audio_track(message: Message, url: str, platform, lang: str):
    """Если в этом чате включён тумблер «Присылать аудио к слайдшоу» — шлём отдельным
    сообщением музыку поста. Вызывается ТОЛЬКО когда слайдшоу отдано как ФОТО (у фото нет
    звука, поэтому музыку докладываем); к видео не применяется — там звук уже внутри."""
    async with SessionLocal() as session:
        if not await get_audio_track(session, message.chat.id):
            return

    # Быстрый кэш по ссылке — мгновенно и БЕЗ запроса к TikTok (частый случай:
    # та же ссылка; устойчиво к сбоям API TikTok).
    cache = Cache(url, "audiotrack")
    if await job.try_cached(cache, message.reply_audio):
        return

    # Для TikTok — запасной ключ по номеру видео (дедуп разных коротких ссылок).
    # Номер берём из данных поста (они уже в памяти после показа видео).
    if platform == Platform.TIKTOK:
        try:
            info = await asyncio.to_thread(tiktok.fetch_tiktok, url)
            cache = Cache(f"tt:{info['id']}", "audiotrack", also=(url,))
            if await job.try_cached(cache, message.reply_audio):
                return
        except Exception:
            pass  # не смогли определить номер — откатываемся на ссылку

    async def work(paths):
        result = await asyncio.to_thread(extract_audio_track, url, platform)
        if not result:
            return None  # нет звука или не удалось извлечь — молча пропускаем (это бонус)
        path, title = paths.keep(result[0]), result[1]
        sent = await message.reply_audio(
            await tg_files.input_file_async(path, await _nice_name(path, quality="")), title=title)
        return sent.audio.file_id if sent.audio else None

    await job.produce(cache=cache, work=work, on_error=_quiet, label="аудиодорожка")


async def _handle_tiktok(message: Message, url: str, lang: str):
    """TikTok: обычное видео — сразу; слайдшоу — спрашиваем формат (видео/фото)."""
    # Быстрый кэш ПО ССЫЛКЕ — мгновенно и БЕЗ запроса к TikTok. Частый случай:
    # переслали ту же ссылку. Устойчиво к сбоям API TikTok (он иногда отвечает
    # ошибкой на частые запросы). Проверяем все возможные форматы поста.
    for ck in ("tt_auto", "tt_video", "tt_photos"):
        if await job.try_cached(Cache(url, ck), lambda v: _send_cached_post(message, v)):
            return

    # По ссылке не нашли — узнаём данные поста (запрос к TikTok, кэшируется в памяти):
    # из них берём НАСТОЯЩИЙ номер видео — по нему кэшируем как запасной ключ.
    # При включённом сжатии HD-вариант не просим: сервис готовит его дольше, а мы всё
    # равно возьмём обычное качество. Настройку читаем заранее, до запроса.
    want_hd = await _shorts_cap(message.chat) is None
    try:
        info = await asyncio.to_thread(tiktok.fetch_tiktok, url, want_hd)
    except Exception as e:
        logger.exception("TikTok fetch failed")
        if await home_tunnel.check_now():   # ВРЕМЕННО home_tunnel: дом выключен – молчим
            return
        await message.reply(limits.friendly_error(e, lang))
        return

    # Ключ кэша — по номеру видео (info["id"]), а не по тексту ссылки. TikTok на одно
    # и то же видео выдаёт РАЗНЫЕ короткие ссылки (vt.tiktok.com/…); по тексту они
    # выглядят разными и раньше качались повторно. По номеру видео — один раз.
    cache_url = f"tt:{info['id']}"

    # Слайдшоу — по настройке /setconfig этого чата: video (сразу видео), photos (сразу
    # фото) или ask (кнопки выбора; их слушает только приславший ссылку). Дефолт зависит
    # от типа чата: в группе — video, в личке — ask (там выбор удобнее по умолчанию).
    compress = False          # у слайдшоу выбирать нечего — сжатие только для видео
    if info["kind"] == "slideshow":
        default_mode = "ask" if message.chat.type == "private" else "video"
        async with SessionLocal() as session:
            ss_mode = await get_slideshow_mode(session, message.chat.id, default=default_mode)

        if ss_mode == "ask":
            # В базу — ссылка и номер видео, данные поста перезапрашиваются у TikTok.
            sid = await TIKTOK.open(message, url, memory={"info": info},
                                    saved={"cache_url": cache_url})
            await message.reply(
                t("tt_slideshow_ask", lang),
                reply_markup=build_tiktok_slideshow_keyboard(sid, lang),
            )
            return
        if ss_mode == "photos":
            mode, cache_key = "photos", "tt_photos"
        else:
            mode, cache_key = "video", "tt_video"
    else:
        # Обычное видео / Live. Сжатие применимо только здесь (у слайдшоу выбирать
        # нечего), поэтому и ключ кэша разделяем только для этой ветки.
        compress = not want_hd
        mode, cache_key = "auto", ("tt_auto_c" if compress else "tt_auto")

    async def work(paths):
        files = paths.keep(await asyncio.to_thread(tiktok.download_from, info, mode, compress))
        tokens = await _send_media_files(message, files, lang)
        if mode == "photos":     # у фото нет звука — доложим музыку слайдшоу (если вкл)
            await _maybe_send_audio_track(message, url, Platform.TIKTOK, lang)
        return "\n".join(tokens) or None

    # Кэш по номеру видео + дедуп: параллельные запросы одного видео (в т.ч. с разными
    # короткими ссылками) ждут ведущего и берут готовое из кэша. Сохраняем и по ссылке —
    # чтобы повтор той же ссылки не спрашивал TikTok вовсе.
    await job.run(cache=Cache(cache_url, cache_key, also=(url,)),
                  send=lambda v: _send_cached_post(message, v), work=work,
                  on_error=_reply_error(message, lang), tell=message, lang=lang,
                  label=f"TikTok {cache_url}", dedupe=True)


@router.callback_query(F.data.startswith("ttdl:"))
async def handle_tiktok_slideshow(callback: CallbackQuery):
    """Выбран формат слайдшоу TikTok: 'video' (со звуком) или 'photos' (отдельные фото)."""
    lang = lang_of(callback.from_user)
    _, mode, sid = callback.data.split(":")
    if mode not in ("video", "photos"):
        await callback.answer(t("link_expired", lang), show_alert=True)
        return
    # Кнопки слушаются только у того, кто прислал ссылку (важно для групп в режиме
    # «Выбор»). Чужое нажатие тихо гасится внутри for_click.
    entry = await TIKTOK.for_click(callback, sid, lang)
    if not entry:
        return

    url, info = entry["url"], entry["info"]
    # Одно нажатие — один результат: пока это скачивание идёт, повторные нажатия той
    # же кнопки только сообщают «уже качаю», а не запускают вторую отправку.
    if not _claim(callback.message.chat.id, sid, mode):
        await callback.answer(t("already_downloading", lang), show_alert=False)
        return
    try:
        alerts.current_request.set(f"tiktok слайдшоу [{mode}]: {url}")  # контекст для тревог
        # Ключ по номеру видео (как в _handle_tiktok): устойчив к разным коротким ссылкам.
        cache = Cache(entry.get("cache_url") or f"tt:{info['id']}", "tt_" + mode, also=(url,))

        # Отвечаем на исходное сообщение пользователя, а не на своё с кнопками: своё мы
        # тут же удаляем, и ответ на него повис бы с пометкой «Удалённое сообщение».
        # Вопрос с кнопками отправлен реплаем, поэтому оригинал лежит в reply_to_message.
        target = callback.message.reply_to_message or callback.message
        await callback.answer()

        # Кэш выбранного формата — отдаём мгновенно
        if await job.try_cached(cache, lambda v: _send_cached_post(target, v)):
            await safe_delete(callback.message)
            return

        async def work(paths):
            files = paths.keep(await asyncio.to_thread(tiktok.download_from, info, mode))
            tokens = await _send_media_files(target, files, lang)
            await safe_delete(callback.message)  # убираем сообщение с кнопками
            if mode == "photos":     # выбрали «Фото» — у них нет звука, доложим музыку (если вкл)
                await _maybe_send_audio_track(target, url, Platform.TIKTOK, lang)
            return "\n".join(tokens) or None

        async def on_error(e):
            await safe_edit(callback.message, limits.friendly_error(e, lang))

        await job.produce(cache=cache, work=work, on_error=on_error,
                          label=f"слайдшоу TikTok [{mode}]")
    finally:
        _unclaim(callback.message.chat.id, sid, mode)
