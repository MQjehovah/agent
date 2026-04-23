import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from autonomous.eventbus import EventBus
from autonomous.perceiver import Perceiver


@pytest.mark.asyncio
async def test_handle_dingtalk_message():
    bus = EventBus()
    perceiver = Perceiver(event_bus=bus, agent=MagicMock())
    await perceiver.handle_dingtalk_message({
        "text": "帮我查一下设备状态",
        "sender_nick": "张三",
    })
    event = await asyncio.wait_for(bus.get(), timeout=1.0)
    assert event.type == "user_message"
    assert event.source == "dingtalk"
    assert event.payload["sender_nick"] == "张三"


@pytest.mark.asyncio
async def test_handle_webhook():
    bus = EventBus()
    perceiver = Perceiver(event_bus=bus, agent=MagicMock())
    await perceiver.handle_webhook({"source": "monitor", "alert": "cpu_high"})
    event = await asyncio.wait_for(bus.get(), timeout=1.0)
    assert event.type == "webhook"
    assert event.source == "monitor"


@pytest.mark.asyncio
async def test_handle_schedule():
    bus = EventBus()
    perceiver = Perceiver(event_bus=bus, agent=MagicMock())
    await perceiver.handle_schedule({"name": "每日巡检", "task": "巡检所有设备"})
    event = await asyncio.wait_for(bus.get(), timeout=1.0)
    assert event.type == "schedule_fired"
    assert event.source == "scheduler"


@pytest.mark.asyncio
async def test_resolve_goal_from_user_message():
    bus = EventBus()
    agent = MagicMock()
    agent.client = MagicMock()
    agent.client.chat = AsyncMock(return_value=MagicMock(
        choices=[MagicMock(message=MagicMock(
            content='{"is_goal": true, "title": "巡检设备", "description": "检查所有设备运行状态"}'
        ))]
    ))
    perceiver = Perceiver(event_bus=bus, agent=agent)
    goal_data = await perceiver.resolve_goal_from_event(
        type="user_message", payload={"text": "帮我做一次设备巡检"}
    )
    assert goal_data is not None
    assert goal_data["is_goal"] is True
