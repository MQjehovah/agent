"""P2 测试：用量统计多维化 + 持久化 + 归因。

验证 track() 从 current_run() 取 user/session/agent 归因、flush() 落库并清空内存、
按维度聚合、settings 定价覆盖、以及 web 路由透传真实 user_id（修复归因缺口）。
"""
import json
import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import storage as storage_mod  # noqa: E402
from agent import RunContext, _current_run  # noqa: E402
from storage import Storage  # noqa: E402
from usage import MODEL_PRICING, UsageRecord, UsageTracker, _resolve_pricing  # noqa: E402

# ── 归因 ──

def test_track_attributes_attribution_from_current_run():
    tracker = UsageTracker()
    ctx = RunContext(user_id="web:42", agent_id="设备运维")
    ctx.session = MagicMock()
    ctx.session.session_id = "sess-1"
    token = _current_run.set(ctx)
    try:
        tracker.track("glm-5", {"prompt_tokens": 100, "completion_tokens": 50})
    finally:
        _current_run.reset(token)
    r = tracker.records[0]
    assert r.user_id == "web:42"
    assert r.session_id == "sess-1"
    assert r.agent_id == "设备运维"


def test_track_defaults_to_system_outside_run():
    tracker = UsageTracker()
    tracker.track("glm-5", {"prompt_tokens": 10, "completion_tokens": 5})
    assert tracker.records[0].user_id == "system"


# ── 持久化 ──

def test_flush_persists_to_storage_and_clears_memory(tmp_path):
    s = Storage(str(tmp_path))
    tracker = UsageTracker()
    orig = storage_mod._storage_instance
    storage_mod._storage_instance = s
    try:
        tracker.track("glm-5", {"prompt_tokens": 100, "completion_tokens": 50})
        tracker.track("glm-4", {"prompt_tokens": 200, "completion_tokens": 100})
        assert tracker.flush() == 2
        assert len(tracker.records) == 0  # 已清空
        rows = s.query_usage(limit=10)
        assert len(rows) == 2
        assert {r["model"] for r in rows} == {"glm-5", "glm-4"}
    finally:
        storage_mod._storage_instance = orig
        s.close()


def test_flush_noop_without_storage_keeps_records():
    tracker = UsageTracker()
    orig = storage_mod._storage_instance
    storage_mod._storage_instance = None
    try:
        tracker.track("glm-5", {"prompt_tokens": 10, "completion_tokens": 5})
        assert tracker.flush() == 0
        assert len(tracker.records) == 1  # 仍在内存
    finally:
        storage_mod._storage_instance = orig


# ── 聚合 ──

def test_summary_by_dimensions():
    tracker = UsageTracker()
    tracker.records = [
        UsageRecord(datetime.now(), "glm-5", 100, 50, 0.01, user_id="web:1", session_id="s1", agent_id="A"),
        UsageRecord(datetime.now(), "glm-5", 200, 100, 0.02, user_id="web:1", session_id="s2", agent_id="A"),
        UsageRecord(datetime.now(), "glm-4", 50, 25, 0.005, user_id="web:2", session_id="s3", agent_id="B"),
    ]
    by_user = tracker.get_summary_by_user()
    assert by_user["web:1"]["calls"] == 2
    assert by_user["web:2"]["calls"] == 1
    by_agent = tracker.get_summary_by_agent()
    assert by_agent["A"]["calls"] == 2
    assert by_agent["B"]["calls"] == 1


# ── 定价 ──

def test_resolve_pricing_settings_override(tmp_path):
    from settings import init_settings
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"cost": {"pricing": {"glm-5": {"input": 5.0, "output": 15.0}}}}),
        encoding="utf-8",
    )
    init_settings(str(tmp_path))
    assert _resolve_pricing("glm-5") == {"input": 5.0, "output": 15.0}
    # 未配置的模型回退 MODEL_PRICING
    assert _resolve_pricing("glm-4") == MODEL_PRICING["glm-4"]


def test_resolve_pricing_fallback_when_settings_unset():
    # settings 由 conftest autouse 用空配置初始化 → 回退 MODEL_PRICING
    assert _resolve_pricing("glm-4-flash") == MODEL_PRICING["glm-4-flash"]
    # 未知模型兜底 0
    assert _resolve_pricing("unknown-model") == {"input": 0, "output": 0}


# ── web 归因透传（修复 web:admin 归因缺口）──

@pytest.mark.asyncio
async def test_web_router_passes_user_id_to_agent():
    from channels import MessageRouter
    agent = MagicMock()
    result = MagicMock()
    result.result = "ok"
    agent.run = AsyncMock(return_value=result)
    router = MessageRouter(agent)
    await router.route("hi", channel="web", session_id="web:s1",
                       user_id="web:42", user_name="张三")
    kwargs = agent.run.call_args.kwargs
    assert kwargs["user_id"] == "web:42"
    assert kwargs["user_name"] == "张三"


@pytest.mark.asyncio
async def test_router_defaults_channel_admin_when_no_user_id():
    from channels import MessageRouter
    agent = MagicMock()
    result = MagicMock()
    result.result = "ok"
    agent.run = AsyncMock(return_value=result)
    router = MessageRouter(agent)
    await router.route("hi", channel="webhook", session_id="webhook:s1")
    # 未显式传 user_id 时兜底为 {channel}:admin（webhook 渠道级归因）
    assert agent.run.call_args.kwargs["user_id"] == "webhook:admin"
