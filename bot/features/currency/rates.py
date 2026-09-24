"""
Курсы валют. Тянем все курсы одним запросом (база USD) с бесплатного API без ключа,
держим в памяти и обновляем не чаще раза в час. Если API недоступен — отдаём
последний известный курс (лучше слегка устаревший, чем ошибка).
"""
import asyncio
import logging
import time

from bot.utils import net

logger = logging.getLogger(__name__)

# open.er-api.com — бесплатно, без ключа, отдаёт все курсы к USD одним ответом.
_URL = "https://open.er-api.com/v6/latest/USD"
_TTL = 3600  # секунд: как часто обновляем курсы

_rates: dict[str, float] = {}   # код -> сколько единиц валюты в 1 USD
_fetched_at: float = 0.0
_lock = asyncio.Lock()


async def _ensure_fresh() -> None:
    """Обновляет курсы, если они устарели. При ошибке оставляет прошлые."""
    if _rates and (time.time() - _fetched_at) < _TTL:
        return
    async with _lock:
        if _rates and (time.time() - _fetched_at) < _TTL:
            return
        try:
            data = await asyncio.to_thread(lambda: net.session().get(_URL, timeout=15).json())
            if data.get("result") == "success" and data.get("rates"):
                globals()["_rates"] = data["rates"]
                globals()["_fetched_at"] = time.time()
                logger.info("Курсы валют обновлены (%d валют)", len(data["rates"]))
        except Exception:
            logger.exception("Не удалось обновить курсы валют — используем прошлые")


async def convert(amount: float, frm: str, to_codes: list[str]) -> dict[str, float] | None:
    """Конвертирует сумму из frm в каждую из to_codes через базу USD.
    None — если курсов нет вовсе или неизвестна исходная валюта."""
    await _ensure_fresh()
    if not _rates or frm not in _rates:
        return None
    usd = amount / _rates[frm]           # сумма в долларах
    out: dict[str, float] = {}
    for code in to_codes:
        rate = _rates.get(code)
        if rate:
            out[code] = usd * rate
    return out
