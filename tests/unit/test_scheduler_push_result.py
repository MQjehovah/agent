import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import pytest

from plugins.scheduler import SchedulerPlugin


class FakePluginManager:
    def __init__(self, plugins):
        self._plugins = plugins

    def get_plugin(self, name):
        return self._plugins.get(name)


class FakeDingTalk:
    def __init__(self):
        self.sent = []

    async def _send_text(self, text, local_user_id=None):
        self.sent.append((text, local_user_id))
        return "ok"


class FakeFeishuClient:
    def __init__(self):
        self.sent = []

    async def send_text_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return {"code": 0}


class FakeFeishu:
    def __init__(self):
        self._client = FakeFeishuClient()


@pytest.mark.asyncio
async def test_push_result_dingtalk():
    sp = SchedulerPlugin()
    dt = FakeDingTalk()
    sp.plugin_manager = FakePluginManager({"dingtalk": dt})
    await sp._push_result(
        {"session_id": "dingtalk:c0:u0", "user_id": "dingtalk:staff1"},
        "任务完成",
    )
    assert dt.sent == [("任务完成", "dingtalk:staff1")]


@pytest.mark.asyncio
async def test_push_result_feishu():
    sp = SchedulerPlugin()
    fs = FakeFeishu()
    sp.plugin_manager = FakePluginManager({"feishu": fs})
    await sp._push_result({"session_id": "feishu:chat1:u1"}, "你好")
    assert fs._client.sent == [("chat1", "你好")]


@pytest.mark.asyncio
async def test_push_result_skipped_without_session():
    sp = SchedulerPlugin()
    dt = FakeDingTalk()
    sp.plugin_manager = FakePluginManager({"dingtalk": dt})
    await sp._push_result({"session_id": ""}, "任务完成")
    await sp._push_result({}, "任务完成")
    assert dt.sent == []


@pytest.mark.asyncio
async def test_push_result_empty_result():
    sp = SchedulerPlugin()
    dt = FakeDingTalk()
    sp.plugin_manager = FakePluginManager({"dingtalk": dt})
    await sp._push_result({"session_id": "dingtalk:c0:u0"}, "")
    assert dt.sent == []
