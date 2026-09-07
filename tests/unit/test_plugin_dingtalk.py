import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from plugins.dingtalk import (
    NOT_PROVISIONED_REPLY,
    AgentChatbotHandler,
    DingTalkConfig,
    DingTalkPlugin,
    format_dingtalk_session_id,
    resolve_dingtalk_staff_id,
)
from security.rbac import RBACManager, UserNotProvisionedError
from storage.storage import Storage

# ---------------- dingtalk_stream 桩(测试不安装 SDK) ----------------

class _FakeText:
    def __init__(self, content=""):
        self.content = content


class _FakeIncomingMessage:
    def __init__(self, data):
        d = data or {}
        self.sender_id = d.get("sender_id", "")
        self.sender_staff_id = d.get("sender_staff_id", "")
        self.staff_id = d.get("staff_id", "")
        self.sender_nick = d.get("sender_nick", "")
        self.conversation_id = d.get("conversation_id", "")
        self.robot_code = d.get("robot_code", "")
        self.text = _FakeText(d.get("content", ""))


class _FakeChatbotMessage:
    TOPIC = "/chat/bot"

    @staticmethod
    def from_dict(data):
        return _FakeIncomingMessage(data)


class _FakeAckMessage:
    STATUS_OK = 200


@pytest.fixture(autouse=True)
def _fake_dingtalk_stream():
    """把 dingtalk_stream 换成桩模块，覆盖插件内的惰性 import。"""
    mod = types.ModuleType("dingtalk_stream")
    mod.ChatbotMessage = _FakeChatbotMessage
    mod.AckMessage = _FakeAckMessage
    sys.modules["dingtalk_stream"] = mod
    yield mod


@pytest.fixture
def storage(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return Storage(str(ws))


def _plugin():
    return DingTalkPlugin(config_path="/nonexistent/dingtalk.json")


def _handler(plugin, router=None):
    handler = AgentChatbotHandler(plugin)
    if router is not None:
        plugin.plugin_manager = MagicMock()
        plugin.plugin_manager.router = router
    return handler


def _msg_data(**overrides):
    data = {
        "sender_id": "sender-x",
        "sender_staff_id": "staff-1",
        "sender_nick": "张三",
        "conversation_id": "conv-1",
        "robot_code": "rc-1",
        "content": "你好",
    }
    data.update(overrides)
    return data


# ---------------- 配置 / 初始化 ----------------

def test_config_load_defaults():
    config = DingTalkConfig()
    assert config.enabled is True
    assert config.stream.client_id == ""
    assert config.stream.client_secret == ""


def test_config_load_from_dict():
    config = DingTalkConfig()
    config.load_from_dict({
        "enabled": True,
        "stream": {"client_id": "cli_test", "client_secret": "secret_test"},
    })
    assert config.stream.client_id == "cli_test"
    assert config.stream.client_secret == "secret_test"


def test_plugin_init_no_config():
    plugin = _plugin()
    assert plugin.name == "dingtalk"
    assert plugin.enabled is True
    assert plugin.sessions == {}
    assert plugin._session_by_key == {}


# ---------------- 会话命名 / 复用 ----------------

def test_format_dingtalk_session_id():
    assert format_dingtalk_session_id(7, "a1b2c3d4") == "dingtalk:7:a1b2c3d4"
    assert format_dingtalk_session_id("7", "deadbeef").startswith("dingtalk:7:")


def test_get_session_namespace_and_reuse():
    plugin = _plugin()
    s1 = plugin.get_session(conversation_id="conv1", agent_uid=7,
                            sender_nick="张三", robot_code="rc1", role="admin")
    assert s1.session_id.startswith("dingtalk:7:")
    assert s1.conversation_id == "conv1"
    assert s1.agent_uid == "7"
    assert s1.role == "admin"

    # 同人同会话复用同一 session，不重复建
    s2 = plugin.get_session(conversation_id="conv1", agent_uid=7,
                            sender_nick="张三", robot_code="rc1")
    assert s2 is s1
    assert len(plugin.sessions) == 1
    assert len(plugin._session_by_key) == 1

    # 同人不同会话 → 新建
    s3 = plugin.get_session(conversation_id="conv2", agent_uid=7,
                            sender_nick="张三", robot_code="rc1")
    assert s3 is not s1
    assert s3.session_id.startswith("dingtalk:7:")
    assert len(plugin.sessions) == 2

    # 不同 agent 用户同一会话 → 隔离，互不串上下文
    s4 = plugin.get_session(conversation_id="conv1", agent_uid=8,
                            sender_nick="李四", robot_code="rc1")
    assert s4 is not s1
    assert s4.session_id.startswith("dingtalk:8:")
    assert len(plugin.sessions) == 3


# ---------------- staff id 反查(主动推送用) ----------------

def test_resolve_dingtalk_staff_id(storage):
    rbac = RBACManager(storage)
    uid = rbac.create_user(name="张三", department="技术部", role="default")
    rbac.bind_identity(uid, "dingtalk", "staff_abc")

    # 新语义: tag = dingtalk:{agent_user.id} → 反查绑定 staff id
    assert resolve_dingtalk_staff_id(storage, f"dingtalk:{uid}") == "staff_abc"
    assert resolve_dingtalk_staff_id(storage, f"dingtalk:{uid + 999}") is None
    assert resolve_dingtalk_staff_id(storage, "") is None
    assert resolve_dingtalk_staff_id(storage, None) is None

    # 兼容历史: 直接以 dingtalk staff id 作为 tag(需已存在于绑定表)
    uid2 = rbac.create_user(name="李四", department="运维部", role="default")
    rbac.bind_identity(uid2, "dingtalk", "888")
    assert resolve_dingtalk_staff_id(storage, "dingtalk:888") == "888"


# ---------------- rbac.require_user ----------------

def test_require_user_unbound_raises(storage):
    rbac = RBACManager(storage)
    with pytest.raises(UserNotProvisionedError):
        rbac.require_user("dingtalk", "no_such_staff", fallback_name="路人")

    # cli 恒放行为管理员
    info = rbac.require_user("cli", "whatever", fallback_name="管理员")
    assert info["role"] == "admin"


def test_require_user_bound_ok_and_disabled_raises(storage):
    rbac = RBACManager(storage)
    uid = rbac.create_user(name="王五", department="产品部", role="admin")
    rbac.bind_identity(uid, "dingtalk", "staff_ok")

    info = rbac.require_user("dingtalk", "staff_ok", fallback_name="王五")
    assert info["user_id"] == uid
    assert info["role"] == "admin"
    assert info["user_name"] == "王五"

    # 禁用后与未开户同语义: 拒绝
    rbac.disable_user(uid)
    with pytest.raises(UserNotProvisionedError):
        rbac.require_user("dingtalk", "staff_ok")


# ---------------- process: 未开户拒绝 ----------------

async def test_process_rejects_unbound_user(storage):
    with patch("storage.storage.get_storage", return_value=storage):
        plugin = _plugin()
        handler = _handler(plugin)
        handler.reply_text = MagicMock()

        callback = SimpleNamespace(data=_msg_data(sender_staff_id="unbound_staff"))
        code, status = await handler.process(callback)

    assert code == 200
    assert status == "OK"
    assert handler.reply_text.called
    args, kwargs = handler.reply_text.call_args
    assert args[0] == NOT_PROVISIONED_REPLY
    assert kwargs.get("msgtype") == "text"
    # 拒绝路径不创建会话、不路由
    assert plugin.sessions == {}


async def test_process_rejects_on_resolve_exception(storage):
    with patch("storage.storage.get_storage", side_effect=RuntimeError("db down")):
        plugin = _plugin()
        handler = _handler(plugin)
        handler.reply_text = MagicMock()

        callback = SimpleNamespace(data=_msg_data())
        code, _ = await handler.process(callback)

    assert code == 200
    assert handler.reply_text.called
    assert plugin.sessions == {}


# ---------------- process: 已开户用户正常路由 ----------------

async def test_process_resolved_user_routes_with_agent_tag(storage):
    rbac = RBACManager(storage)
    uid = rbac.create_user(name="张三", department="技术部", role="admin")
    rbac.bind_identity(uid, "dingtalk", "staff-1")

    router = MagicMock()
    router.route = AsyncMock(return_value=SimpleNamespace(result="已处理"))
    with patch("storage.storage.get_storage", return_value=storage):
        plugin = _plugin()
        handler = _handler(plugin, router=router)
        handler.reply_text = MagicMock()

        callback = SimpleNamespace(data=_msg_data())
        code, _ = await handler.process(callback)
        # 同人同会话再发一条 → 复用同一 session
        code2, _ = await handler.process(callback)

    assert code == 200
    assert code2 == 200
    assert router.route.await_count == 2
    _, kwargs = router.route.await_args
    assert kwargs["channel"] == "dingtalk"
    # 路由身份必须为 dingtalk:{agent_user.id}，绝不再是 dingtalk:{staff_id}
    assert kwargs["user_id"] == f"dingtalk:{uid}"
    assert kwargs["user_name"] == "张三"
    assert kwargs["role"] == "admin"
    assert kwargs["session_id"].startswith(f"dingtalk:{uid}:")

    # 两轮会话 id 相同且仅建一个会话
    sid0 = router.route.await_args_list[0].kwargs["session_id"]
    sid1 = router.route.await_args_list[1].kwargs["session_id"]
    assert sid0 == sid1
    assert len(plugin.sessions) == 1
    assert plugin.sessions[sid0].conversation_id == "conv-1"

    # 回复内容来自 agent
    args, _ = handler.reply_text.call_args
    assert args[0] == "已处理"
