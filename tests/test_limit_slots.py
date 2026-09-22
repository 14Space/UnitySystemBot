"""Слот очереди нельзя занять навсегда, и ожидание не бывает молчаливым.

23.09.2026 бот перестал отвечать на ссылки ВОВСЕ: слот заняли и не отпустили
(отправка полоски прогресса стояла вне защищённого блока и не прошла), а все
следующие загрузки молча встали в очередь за мёртвым держателем. Со стороны бот
выглядел живым и просто не отвечал — ни ошибки, ни строчки в логе.
"""
import asyncio

import pytest

from bot.utils import limits


@pytest.fixture
def limiter(monkeypatch):
    """Свой ограничитель на каждый тест: общий хранит состояние между проверками."""
    fresh = limits._SmartLimiter()
    monkeypatch.setattr(limits, "_limiter", fresh)
    return fresh


def test_slots_are_freed_when_the_holder_is_lost(limiter, monkeypatch):
    """Держатель пропал — слот обязан вернуться, иначе очередь встанет навсегда."""
    monkeypatch.setattr(limits, "MAX_HOLD_SECONDS", 0)   # всё занятое сразу «потеряно»

    async def scenario():
        for _ in range(limits.HEAVY_MAX):
            await limits.acquire(limits.HEAVY)
        assert limits.queue_is_full(limits.HEAVY)
        # Никто не освобождает — но ждать вечно мы не должны.
        await asyncio.wait_for(limits.acquire(limits.HEAVY), timeout=5)

    asyncio.run(scenario())


def test_working_holder_is_not_taken_away(limiter):
    """Живую задачу трогать нельзя: страховка не должна пускать лишних."""
    async def scenario():
        for _ in range(limits.HEAVY_MAX):
            await limits.acquire(limits.HEAVY)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(limits.acquire(limits.HEAVY), timeout=0.3)

    asyncio.run(scenario())


def test_release_frees_a_slot(limiter):
    async def scenario():
        await limits.acquire(limits.LIGHT)
        st = limits.state()[limits.LIGHT]
        assert st["active"] == 1
        await limits.release(limits.LIGHT)
        assert limits.state()[limits.LIGHT]["active"] == 0

    asyncio.run(scenario())


def test_state_shows_how_long_a_slot_is_held(limiter):
    async def scenario():
        await limits.acquire(limits.TRANSCRIBE)
        st = limits.state()[limits.TRANSCRIBE]
        assert st["active"] == 1 and st["cap"] >= 1 and st["oldest_sec"] >= 0

    asyncio.run(scenario())


def test_queue_wait_tells_the_person(limiter):
    """Занято — человек должен об этом узнать, а не смотреть в тишину."""
    said = []

    class FakeNotice:
        async def delete(self):
            said.append("убрал")

    class FakeMessage:
        async def reply(self, text):
            said.append(text)
            return FakeNotice()

    async def scenario():
        for _ in range(limits.HEAVY_MAX):
            await limits.acquire(limits.HEAVY)

        waiting = asyncio.create_task(
            limits.acquire_or_tell(limits.HEAVY, FakeMessage(), "ru"))
        await asyncio.sleep(0.05)
        assert said and "очеред" in said[0]          # предупредили сразу
        await limits.release(limits.HEAVY)           # место освободилось
        await asyncio.wait_for(waiting, timeout=5)
        assert said[-1] == "убрал"                   # и предупреждение убрали

    asyncio.run(scenario())


def test_free_queue_says_nothing(limiter):
    """Когда место есть, никаких сообщений быть не должно."""
    said = []

    class FakeMessage:
        async def reply(self, text):
            said.append(text)

    async def scenario():
        await limits.acquire_or_tell(limits.LIGHT, FakeMessage(), "ru")
        assert said == []

    asyncio.run(scenario())
