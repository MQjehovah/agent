import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import asyncio

import pytest

from autonomous.eventbus import Event, EventBus


def test_event_creation():
    event = Event(type="user_message", source="dingtalk", payload={"text": "hello"})
    assert event.type == "user_message"
    assert event.priority == 3


def test_event_priority_ordering():
    e1 = Event(type="a", source="test", payload={}, priority=5)
    e2 = Event(type="b", source="test", payload={}, priority=1)
    e3 = Event(type="c", source="test", payload={}, priority=3)
    assert e2 < e3 < e1


@pytest.mark.asyncio
async def test_eventbus_publish_and_get():
    bus = EventBus()
    event = Event(type="test", source="unit", payload={"key": "value"})
    await bus.publish(event)
    got = await asyncio.wait_for(bus.get(), timeout=1.0)
    assert got.type == "test"
    assert got.payload["key"] == "value"


@pytest.mark.asyncio
async def test_eventbus_priority_order():
    bus = EventBus()
    await bus.publish(Event(type="low", source="test", payload={}, priority=1))
    await bus.publish(Event(type="high", source="test", payload={}, priority=5))
    await bus.publish(Event(type="mid", source="test", payload={}, priority=3))

    first = await asyncio.wait_for(bus.get(), timeout=1.0)
    assert first.type == "high"

    second = await asyncio.wait_for(bus.get(), timeout=1.0)
    assert second.type == "mid"

    third = await asyncio.wait_for(bus.get(), timeout=1.0)
    assert third.type == "low"


@pytest.mark.asyncio
async def test_eventbus_size():
    bus = EventBus()
    await bus.publish(Event(type="a", source="t", payload={}))
    await bus.publish(Event(type="b", source="t", payload={}))
    assert bus.size() == 2
