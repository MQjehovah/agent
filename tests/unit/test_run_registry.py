"""跨渠道运行登记（channels.run_registry + MessageRouter.route hook）。

- register_run/unregister_run/running_sessions 纯逻辑生命周期
- route 在执行期间登记、结束（含异常/取消）后注销
- cli 渠道不登记（本机交互不属线上运行中）
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from channels import run_registry  # noqa: E402
from channels.router import MessageRouter  # noqa: E402


def _reset():
    run_registry.reset()


# ---------------- 登记纯逻辑 ----------------

def test_run_registry_lifecycle():
    _reset()
    try:
        assert run_registry.running_sessions() == []
        run_registry.register_run("dingtalk:7:s1", "dingtalk", "dingtalk:7",
                                  started_at="2026-09-07T10:00:00", model="qwen-max")
        run_registry.register_run("web:7:w1", "web", "web:7",
                                  started_at="2026-09-07T10:00:01", model="qwen-max")
        runs = {r["session_id"]: r for r in run_registry.running_sessions()}
        assert set(runs) == {"dingtalk:7:s1", "web:7:w1"}
        assert runs["dingtalk:7:s1"]["tag"] == "dingtalk:7"
        assert runs["dingtalk:7:s1"]["uid"] == "7"
        assert runs["dingtalk:7:s1"]["channel"] == "dingtalk"
        assert runs["dingtalk:7:s1"]["model"] == "qwen-max"

        run_registry.unregister_run("dingtalk:7:s1")
        assert {r["session_id"] for r in run_registry.running_sessions()} == {"web:7:w1"}
        run_registry.unregister_run("dingtalk:7:s1")  # 幂等
        run_registry.unregister_run("web:7:w1")
        assert run_registry.running_sessions() == []
    finally:
        _reset()


def test_run_registry_skips_empty():
    _reset()
    try:
        run_registry.register_run("", "dingtalk", "dingtalk:7")
        run_registry.register_run("dingtalk:7:s1", "", "dingtalk:7")
        assert run_registry.running_sessions() == []
    finally:
        _reset()


def test_run_registry_defaults_tag_and_started_at():
    _reset()
    try:
        run_registry.register_run("feishu:conv:u", "feishu")
        runs = run_registry.running_sessions()
        assert runs and runs[0]["tag"] == "feishu:admin"
        assert runs[0]["started_at"]
    finally:
        _reset()


# ---------------- route hook ----------------

class _Blocker:
    """agent.run 桩：执行开始置 started，等待 release 后返回。"""

    def __init__(self, model="qwen-max"):
        self.client = SimpleNamespace(model=model)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, content, **kwargs):
        self.started.set()
        await self.release.wait()
        return SimpleNamespace(result="ok")


async def test_route_registers_plugin_channel_while_running_and_unregisters_after():
    _reset()
    try:
        blocker = _Blocker()
        router = MessageRouter(blocker)
        task = asyncio.create_task(
            router.route("你好", channel="dingtalk",
                         session_id="dingtalk:7:s1",
                         user_id="dingtalk:7", user_name="张三"))
        await blocker.started.wait()
        runs = {r["session_id"]: r for r in run_registry.running_sessions()}
        assert set(runs) == {"dingtalk:7:s1"}
        assert runs["dingtalk:7:s1"]["tag"] == "dingtalk:7"
        assert runs["dingtalk:7:s1"]["channel"] == "dingtalk"

        blocker.release.set()
        result = await task
        assert result == "ok"
        assert run_registry.running_sessions() == []  # 结束后消失
    finally:
        _reset()


async def test_route_unregisters_when_run_raises():
    _reset()
    try:
        class _Raiser:
            client = SimpleNamespace(model="qwen-max")

            async def run(self, content, **kwargs):
                raise RuntimeError("boom")

        router = MessageRouter(_Raiser())
        with pytest.raises(RuntimeError):
            await router.route("x", channel="dingtalk",
                               session_id="dingtalk:7:s1", user_id="dingtalk:7")
        assert run_registry.running_sessions() == []
    finally:
        _reset()


async def test_route_skips_cli_channel():
    _reset()
    try:
        blocker = _Blocker()
        router = MessageRouter(blocker)
        task = asyncio.create_task(
            router.route("hi", channel="cli", session_id="cli:local1"))
        await blocker.started.wait()
        assert run_registry.running_sessions() == []  # cli 不进运行中
        blocker.release.set()
        await task
        assert run_registry.running_sessions() == []
    finally:
        _reset()


async def test_route_generates_session_when_missing():
    _reset()
    try:
        blocker = _Blocker()
        router = MessageRouter(blocker)
        task = asyncio.create_task(router.route("hi", channel="dingtalk", user_id="dingtalk:7"))
        await blocker.started.wait()
        runs = run_registry.running_sessions()
        assert len(runs) == 1 and runs[0]["session_id"].startswith("dingtalk:")
        blocker.release.set()
        await task
    finally:
        _reset()
