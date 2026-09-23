"""钉钉互动卡片工具确认回路测试(fail-closed)。

覆盖: 允许/拒绝/超时/发送失败/发送异常、重复点击幂等、审计字段、
卡片 payload 纯函数、回调 content 解析、超时环境变量非法回退,
以及插件侧装配(confirm_scope 权限模式/on_confirm 设置与恢复、按 run 派发、
渠道不可用明确文案标记)。
"""
import asyncio
import json
import os
import sys
import types
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from plugins.dingtalk import CardActionHandler, DingTalkPlugin  # noqa: E402
from plugins.dingtalk.confirm import (  # noqa: E402
    AGREE_ACTION_ID,
    CONFIRM_TITLE,
    CONFIRM_UNAVAILABLE_REPLY,
    DEFAULT_CONFIRM_TIMEOUT,
    OUT_TRACK_PREFIX,
    REJECT_ACTION_ID,
    CardActionRegistry,
    build_confirm_card_payload,
    create_card_confirmer,
    parse_card_action,
    parse_out_track_id,
    resolve_confirm_timeout,
)
from security.permissions import PermissionMode  # noqa: E402


def _plugin():
    return DingTalkPlugin(config_path="/nonexistent/dingtalk.json")


# ── 纯函数: 超时解析 ──

def test_resolve_confirm_timeout_default_and_explicit():
    assert resolve_confirm_timeout(None, default=DEFAULT_CONFIRM_TIMEOUT) == DEFAULT_CONFIRM_TIMEOUT
    assert resolve_confirm_timeout(30, default=DEFAULT_CONFIRM_TIMEOUT) == 30.0


def test_resolve_confirm_timeout_env_override_and_invalid_fallback(monkeypatch):
    monkeypatch.setenv("DINGTALK_CONFIRM_TIMEOUT", "45")
    assert resolve_confirm_timeout() == 45.0
    # 非法值(非数字/非正数)一律回退默认, 不放宽为 0/负数(否则等于无等待直接拒绝)
    for bad in ("abc", "", "0", "-5", "nan"):
        monkeypatch.setenv("DINGTALK_CONFIRM_TIMEOUT", bad)
        assert resolve_confirm_timeout() == DEFAULT_CONFIRM_TIMEOUT


# ── 纯函数: 卡片 payload ──

def test_build_confirm_card_payload_buttons_and_truncation():
    payload = build_confirm_card_payload(
        "shell", {"command": "x" * 1000, "path": "/tmp/a"},
        request_id="req-1", context="渠道: 钉钉 / 会话: dingtalk:7:abc",
        max_arg_chars=50)
    assert payload["msgTitle"] == CONFIRM_TITLE
    assert payload["requestId"] == "req-1"
    assert payload["tool"] == "shell"
    assert "工具：shell" in payload["staticMsgContent"]
    assert "渠道: 钉钉 / 会话: dingtalk:7:abc" in payload["staticMsgContent"]
    assert "…" in payload["staticMsgContent"]          # 参数摘要截断
    assert len(payload["argsDigest"]) <= 51
    layout = json.loads(payload["sys_full_json_obj"])
    assert layout["order"] == ["msgTitle", "staticMsgContent", "msgButtons"]
    buttons = layout["msgButtons"]
    assert [b["id"] for b in buttons] == [AGREE_ACTION_ID, REJECT_ACTION_ID]
    assert all(b.get("request") is True for b in buttons)
    assert payload["flowStatus"] == "3"


def test_build_confirm_card_payload_caps_param_value(monkeypatch):
    payload = build_confirm_card_payload(
        "file", {"content": "y" * 5000}, request_id="req-2",
        context="c" * 2000, max_arg_chars=4000)
    # 钉钉卡片单值上限 1KB(留余量 900 字符)
    assert len(payload["staticMsgContent"]) <= 901


# ── 纯函数: 回调解析 ──

def test_parse_out_track_id():
    assert parse_out_track_id(f"{OUT_TRACK_PREFIX}req-9") == "req-9"
    assert parse_out_track_id("other-track") == ""
    assert parse_out_track_id(None) == ""


def test_parse_card_action_variants():
    assert parse_card_action({"cardPrivateData": {"actionIds": ["agree"]}}) is True
    assert parse_card_action({"cardPrivateData": {"actionIds": ["reject"]}}) is False
    assert parse_card_action({"cardPrivateData": {"actionIds": ["同意"]}}) is True
    # 通用卡片布局按钮: 动作 id 在 params
    assert parse_card_action({"cardPrivateData": {"params": {"action": "agree"}}}) is True
    assert parse_card_action({"cardPrivateData": {"params": {"id": "reject"}}}) is False
    # content 为 JSON 字符串(SDK 实际形态)
    assert parse_card_action(json.dumps({"cardPrivateData": {"actionIds": ["agree"]}})) is True
    # 未知动作/无效输入不裁决
    assert parse_card_action({"cardPrivateData": {"actionIds": ["openLink"]}}) is None
    assert parse_card_action({"cardPrivateData": {}}) is None
    assert parse_card_action(None) is None
    assert parse_card_action("not-json") is None


# ── 登记表: 重复点击幂等 ──

async def test_card_action_registry_first_verdict_wins():
    registry = CardActionRegistry()
    registry.register("r1")
    assert registry.resolve("r1", True) is True
    assert registry.resolve("r1", False) is False   # 重复点击忽略, 首个裁决生效
    assert await registry.wait("r1", timeout=0.05) is True
    assert registry.resolve("r1", True) is False    # 已消费后也不再生效
    assert registry.resolve("unknown", True) is False


async def test_card_action_registry_timeout_ignores_late_click():
    registry = CardActionRegistry()
    registry.register("r2")
    assert await registry.wait("r2", timeout=0.01) is None
    assert registry.resolve("r2", True) is False    # 超时后迟到点击被忽略


# ── confirmer: 允许/拒绝/超时/发送失败 fail-closed ──

def _recorder():
    calls = {"sent": [], "waited": [], "audits": []}

    async def send_card(request_id, payload):
        calls["sent"].append((request_id, payload))
        return True

    async def wait_action(request_id, timeout):
        calls["waited"].append((request_id, timeout))
        return None

    def audit(tool, ok, detail):
        calls["audits"].append((tool, ok, detail))

    return calls, send_card, wait_action, audit


async def test_confirmer_allow_returns_true_and_audits():
    calls, send_card, wait_action, audit = _recorder()

    async def wait_allow(request_id, timeout):
        calls["waited"].append((request_id, timeout))
        return True

    confirm = create_card_confirmer(
        send_card=send_card, wait_action=wait_allow, audit=audit, now=lambda: 1000.0)
    assert await confirm("shell", {"command": "rm -rf /tmp/x"}) is True
    assert calls["audits"] == [("shell", True, "approved")]
    assert calls["sent"] and calls["sent"][0][0] == calls["waited"][0][0]
    assert calls["sent"][0][1]["requestId"] == calls["sent"][0][0]


async def test_confirmer_reject_returns_false_and_audits():
    calls, send_card, _, audit = _recorder()

    async def wait_reject(request_id, timeout):
        return False

    confirm = create_card_confirmer(
        send_card=send_card, wait_action=wait_reject, audit=audit)
    assert await confirm("file", {"operation": "write", "path": "a.txt"}) is False
    assert calls["audits"] == [("file", False, "rejected")]


async def test_confirmer_timeout_fail_closed(monkeypatch):
    monkeypatch.setenv("DINGTALK_CONFIRM_TIMEOUT", "7")
    calls, send_card, wait_timeout, audit = _recorder()
    confirm = create_card_confirmer(
        send_card=send_card, wait_action=wait_timeout, audit=audit)
    assert confirm.timeout_seconds == 7.0
    assert await confirm("shell", {}) is False
    assert calls["audits"] == [("shell", False, "timeout")]
    assert calls["waited"] == [(calls["sent"][0][0], 7.0)]


async def test_confirmer_send_failure_fail_closed():
    calls, _, wait_timeout, audit = _recorder()

    async def send_fail(request_id, payload):
        calls["sent"].append((request_id, payload))
        return False

    confirm = create_card_confirmer(
        send_card=send_fail, wait_action=wait_timeout, audit=audit)
    assert await confirm("shell", {}) is False
    assert calls["audits"] == [("shell", False, "send_failed")]
    assert calls["waited"] == []                     # 未发送不等待


async def test_confirmer_send_exception_fail_closed():
    calls, _, wait_timeout, audit = _recorder()

    async def send_boom(request_id, payload):
        raise RuntimeError("network down")

    confirm = create_card_confirmer(
        send_card=send_boom, wait_action=wait_timeout, audit=audit)
    assert await confirm("shell", {}) is False
    assert calls["audits"] == [("shell", False, "send_failed")]


async def test_confirmer_wait_exception_fail_closed():
    calls, send_card, _, audit = _recorder()

    async def wait_boom(request_id, timeout):
        raise RuntimeError("callback broker down")

    confirm = create_card_confirmer(
        send_card=send_card, wait_action=wait_boom, audit=audit)
    assert await confirm("shell", {}) is False
    assert calls["audits"] == [("shell", False, "timeout")]


async def test_confirmer_audit_exception_does_not_break_verdict():
    async def send_card(request_id, payload):
        return True

    async def wait_allow(request_id, timeout):
        return True

    def audit_boom(tool, ok, detail):
        raise RuntimeError("audit sink down")

    confirm = create_card_confirmer(
        send_card=send_card, wait_action=wait_allow, audit=audit_boom)
    assert await confirm("shell", {}) is True


# ── 插件侧: 装配/派发/执行器联动(不触网) ──

class _FakeAgent:
    def __init__(self, mode=PermissionMode.AUTO):
        self.on_confirm = None
        self._permission_config = SimpleNamespace(mode=mode)


async def test_confirm_scope_installs_default_mode_and_restores():
    plugin = _plugin()
    agent = _FakeAgent()
    fallback_calls = []

    async def fallback(name, args):
        fallback_calls.append(name)
        return True

    agent.on_confirm = fallback
    confirmer_calls = []

    async def confirmer(name, args):
        confirmer_calls.append(name)
        return True

    async with plugin.confirm_scope(agent, "dingtalk:7:abc", confirmer):
        assert agent._permission_config.mode == PermissionMode.DEFAULT
        assert callable(agent.on_confirm)
        assert agent.on_confirm is not fallback
        # 当前 run 命中已登记会话 → 用卡片 confirmer
        from agent.core import RunContext, _current_run
        token = _current_run.set(RunContext(conversation_id="dingtalk:7:abc"))
        try:
            assert await agent.on_confirm("shell", {}) is True
        finally:
            _current_run.reset(token)
        # 非本会话(如并发 web run) → 回退原回调, 不误用卡片
        token = _current_run.set(RunContext(conversation_id="web:1:xyz"))
        try:
            assert await agent.on_confirm("shell", {}) is True
        finally:
            _current_run.reset(token)
    assert confirmer_calls == ["shell"]
    assert fallback_calls == ["shell"]
    assert agent._permission_config.mode == PermissionMode.AUTO
    assert agent.on_confirm is fallback


async def test_confirm_scope_keeps_non_auto_mode_and_fails_closed_without_fallback():
    plugin = _plugin()
    agent = _FakeAgent(mode=PermissionMode.PLAN)

    async def confirmer(name, args):
        return True

    async with plugin.confirm_scope(agent, "dingtalk:7:abc", confirmer):
        assert agent._permission_config.mode == PermissionMode.PLAN
        from agent.core import RunContext, _current_run
        token = _current_run.set(RunContext(conversation_id="other:1"))
        try:
            assert await agent.on_confirm("shell", {}) is False   # 无登记且无回退 → 拒绝
        finally:
            _current_run.reset(token)
    assert agent._permission_config.mode == PermissionMode.PLAN
    assert agent.on_confirm is None


async def test_confirm_scope_no_agent_is_noop():
    plugin = _plugin()

    async def confirmer(name, args):
        return True

    async with plugin.confirm_scope(None, "dingtalk:7:abc", confirmer):
        pass
    assert plugin._confirmers == {}


def test_plugin_confirm_unavailable_reply_flag():
    plugin = _plugin()
    audit = plugin._make_confirm_audit("sess-1")
    audit("shell", False, "timeout")
    assert plugin.consume_confirm_unavailable("sess-1") is False
    audit("shell", False, "send_failed")
    assert plugin.consume_confirm_unavailable("sess-1") is True
    assert plugin.consume_confirm_unavailable("sess-1") is False
    assert CONFIRM_UNAVAILABLE_REPLY


async def test_send_confirm_card_fail_closed_when_callback_topic_unavailable():
    plugin = _plugin()
    assert plugin._card_callback_ready is False
    assert await plugin._send_confirm_card("req-1", {"msgTitle": "x"}, "dingtalk:7") is False


# ── 卡片回调入口: 唤醒等待者 + 重复点击幂等 ──

class _FakeCardCallbackMessage:
    @staticmethod
    def from_dict(data):
        return SimpleNamespace(
            card_instance_id=data.get("outTrackId", ""),
            user_id=data.get("userId", ""),
            content=json.loads(data.get("content") or "{}"))

class _FakeAckMessage:
    STATUS_OK = 200

    def __init__(self):
        self.code = None
        self.data = {}
        self.headers = SimpleNamespace(message_id=None, content_type=None)


async def test_card_callback_handler_wakes_waiter_and_first_verdict_wins(monkeypatch):
    stub = types.ModuleType("dingtalk_stream")
    stub.CardCallbackMessage = _FakeCardCallbackMessage
    stub.AckMessage = _FakeAckMessage
    monkeypatch.setitem(sys.modules, "dingtalk_stream", stub)

    plugin = _plugin()
    handler = CardActionHandler(plugin)
    plugin._card_actions.register("req-1")

    def _callback(action_id):
        return SimpleNamespace(
            data={
                "outTrackId": f"{OUT_TRACK_PREFIX}req-1",
                "userId": "staff-1",
                "content": json.dumps({"cardPrivateData": {"actionIds": [action_id]}}),
            },
            headers=SimpleNamespace(message_id="m-1"),
        )

    ack = await handler.raw_process(_callback("agree"))
    assert ack.code == 200
    assert await plugin._card_actions.wait("req-1", timeout=0.05) is True
    # 重复点击(包括相反裁决)不生效: 首个裁决已消费, 幂等返回 False
    ack2 = await handler.raw_process(_callback("reject"))
    assert ack2.code == 200
    assert plugin.resolve_card_action("req-1", False) is False
    # 非本插件 outTrackId 不裁决
    plugin._card_actions.register("req-2")
    foreign = SimpleNamespace(
        data={"outTrackId": "other-track", "userId": "staff-1",
              "content": json.dumps({"cardPrivateData": {"actionIds": ["agree"]}})},
        headers=SimpleNamespace(message_id="m-2"))
    await handler.raw_process(foreign)
    assert await plugin._card_actions.wait("req-2", timeout=0.01) is None


def test_plugin_make_confirmer_binds_context_and_user():
    plugin = _plugin()
    captured = {}

    async def fake_send(request_id, payload, local_user_id):
        captured["payload"] = payload
        captured["local_user_id"] = local_user_id
        return False

    async def fake_wait(request_id, timeout):
        return None

    audit_calls = []
    plugin._wait_card_action = fake_wait
    plugin._send_confirm_card = fake_send
    plugin._make_confirm_audit = lambda session_id: (lambda t, ok, d: audit_calls.append((session_id, t, ok, d)))

    confirm = plugin._make_confirmer("dingtalk:7", "dingtalk:7:abc", "张三")
    result = asyncio.run(confirm("shell", {}))
    assert result is False
    assert captured["local_user_id"] == "dingtalk:7"
    assert "dingtalk:7:abc" in captured["payload"]["staticMsgContent"]
    assert "张三" in captured["payload"]["staticMsgContent"]
    assert audit_calls == [("dingtalk:7:abc", "shell", False, "send_failed")]
