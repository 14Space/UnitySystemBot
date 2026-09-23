"""
Экраны с кнопками: выбор качества, HDRezka, слайдшоу TikTok, список треков.

Экран – это сообщение с кнопками плюс то, что боту нужно помнить до нажатия: чья
это ссылка, есть ли у владельца Premium, данные площадки. Раньше под это было четыре
отдельных хранилища в памяти, копии в базе у трёх из них и три почти одинаковые
функции восстановления после перезапуска. А проверка «кнопку нажал тот, кто прислал
ссылку» была только у TikTok: в группе любой мог нажать чужой выбор качества или
чужую серию HDRezka.

Теперь у каждого экрана одинаковые поля:
  • owner   – кто прислал ссылку; чужие нажатия тихо гасятся;
  • premium – был ли Premium у владельца, когда рисовали кнопки (по нему
              перерисовываются замки на страницах). Право скачать выше 720p всё равно
              проверяется в момент нажатия, по нажавшему (S5).
И одинаковый путь: память → база → restore() конкретного вида.
"""
import logging
import uuid

from bot.database import SessionLocal
from bot.database.repository import save_link_stash, load_link_stash
from bot.utils.i18n import t

logger = logging.getLogger(__name__)

# Чтобы хранилища не росли бесконечно (утечка памяти при долгой работе), держим не
# больше последних N записей – старые выкидываем. Экран, выпавший из памяти, всё равно
# поднимется из базы.
_CAP = 300


def remember(store: dict, key: str, value) -> None:
    store[key] = value
    if len(store) > _CAP:
        for old in list(store.keys())[: len(store) - _CAP]:
            store.pop(old, None)


def new_id() -> str:
    """Короткий id для кнопки: callback_data у Telegram не длиннее 64 байт."""
    return uuid.uuid4().hex[:8]


class Screens:
    """Экраны одного вида.

    restore(saved) – собирает экран заново из того, что лежит в базе (ссылка, чат,
    владелец и payload), когда в памяти его нет: бот перезапускался. None – собрать
    не вышло. Без restore экран поднимается из базы как есть.
    """

    def __init__(self, kind: str, restore=None):
        self.kind = kind
        self._restore = restore
        self._mem: dict[str, dict] = {}

    async def open(self, message, url: str, *, premium: bool = False,
                   memory: dict | None = None, saved: dict | None = None) -> str:
        """Заводит экран и возвращает его id для кнопок.

        memory – то, что живёт только в памяти (метаданные площадки, объект сессии);
        saved – то, что должно пережить перезапуск (уйдёт в базу как payload).
        """
        sid = new_id()
        owner = message.from_user.id if getattr(message, "from_user", None) else None
        base = {"url": url, "chat_id": message.chat.id, "user_msg_id": message.message_id,
                "owner": owner, "premium": premium}
        remember(self._mem, sid, {**base, **(saved or {}), **(memory or {})})
        async with SessionLocal() as session:
            await save_link_stash(session, sid, url, message.chat.id, message.message_id,
                                  premium, kind=self.kind,
                                  payload={"owner": owner, **(saved or {})})
        return sid

    async def get(self, sid: str) -> dict | None:
        """Экран по id: из памяти, а если бот перезапускался – из базы."""
        entry = self._mem.get(sid)
        if entry is not None:
            return entry
        async with SessionLocal() as session:
            saved = await load_link_stash(session, sid)
        if not saved or saved.get("kind") != self.kind:
            return None
        if self._restore is None:
            entry = dict(saved)
        else:
            try:
                entry = await self._restore(saved)
            except Exception:
                logger.warning("Экран %s (%s) не восстановился", sid, self.kind, exc_info=True)
                entry = None
            if entry is None:
                return None
        # Поля, общие для всех экранов, берём из базы – restore о них знать не обязан.
        for key in ("url", "chat_id", "user_msg_id", "owner", "premium"):
            entry.setdefault(key, saved.get(key))
        remember(self._mem, sid, entry)
        logger.info("Экран %s (%s) восстановлен после перезапуска", sid, self.kind)
        return entry

    async def for_click(self, callback, sid: str, lang: str) -> dict | None:
        """Экран для нажатия. None – на нажатие уже ответили (экран устарел или кнопка
        чужая), обработчику делать нечего."""
        entry = await self.get(sid)
        if entry is None:
            await callback.answer(t("link_expired", lang), show_alert=True)
            return None
        owner = entry.get("owner")
        # Кнопки слушаются только того, кто прислал ссылку. Экраны, заведённые до
        # появления этого поля, владельца не знают – их по-прежнему может нажать любой.
        if owner is not None and callback.from_user.id != owner:
            await callback.answer()        # «часики» на кнопке уберутся, работы не будет
            return None
        return entry

    def clear(self) -> None:
        """Забыть всё в памяти (для тестов: «перезапуск»)."""
        self._mem.clear()


# Короткий id -> ссылка, для перехода из inline-режима в личку: deep link короче 64
# символов, а ссылка бывает длиннее. Это не экран – кнопок и владельца у него нет.
INLINE_LINKS: dict[str, str] = {}


def stash_inline_link(url: str) -> str:
    """Сохраняет ссылку под коротким id (для кнопки-перехода из inline в личку)."""
    sid = new_id()
    remember(INLINE_LINKS, sid, url)
    return sid
