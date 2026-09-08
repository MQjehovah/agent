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
    dingtalk_group_root_prefix,
    format_dingtalk_session_id,
    is_group_conversation_id,
    resolve_dingtalk_staff_id,
    sanitize_dingtalk_cid,
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


# ---------------- 会话命名 / 复用(单聊 scope / 群共享根) ----------------

def test_format_dingtalk_session_id():
    assert format_dingtalk_session_id(7, "a1b2c3d4") == "dingtalk:7:a1b2c3d4"
    assert format_dingtalk_session_id("7", "deadbeef").startswith("dingtalk:7:")


def test_group_prefix_helpers():
    """群根前缀：同 cid 稳定映射、含安全子串+hash、仅字母数字可 LIKE 查询。"""
    cid = "cidGROUP+abc/def=="
    assert is_group_conversation_id(cid) is True
    assert is_group_conversation_id("") is False
    assert is_group_conversation_id("conv-1") is False
    assert is_group_conversation_id("cidOnly") is True

    p1 = dingtalk_group_root_prefix(cid)
    p2 = dingtalk_group_root_prefix("cidGROUP+abc/def==")
    assert p1 == p2
    assert p1.startswith("dingtalk_group:cidGROUPabcdef:")
    # cid 安全子串与 hash 段仅含字母数字(可安全拼入 LIKE)；固定字面 _ 与 : 为前缀分隔符
    assert all(c.isalnum() or c in ":_" for c in p1)
    assert dingtalk_group_root_prefix("cidGROUP+abc/def==") != dingtalk_group_root_prefix("cidGROUPabc/def==")
    assert sanitize_dingtalk_cid("cidGROUP+abc/def==") == sanitize_dingtalk_cid("cidGROUPabc/def==")
    assert sanitize_dingtalk_cid("cidGROUP+abc/def==") == "cidGROUPabcdef"


def test_group_judge_prefers_conversation_type():
    """单/群判定优先官方 conversation_type：cid 开头的单聊不得误判为群。"""
    # 群: conversation_type='2' 或 前缀 cid 兜底
    assert is_group_conversation_id("cidX", "2") is True
    assert is_group_conversation_id("", "2") is True
    # 单聊: conversation_type='1'，即使 conversation_id 以 cid 开头(线上单聊常见)
    assert is_group_conversation_id("cidX", "1") is False
    assert is_group_conversation_id("cidX", "") is True   # 无 type 时回退前缀
    # 其它取值视为缺失 -> 回退前缀
    assert is_group_conversation_id("cidX", "unknown") is True


def test_resolve_session_single_scope_and_reuse():
    """单聊按 agent_uid 一个 scope：同人不同 conversation_id 复用同一根；不同人隔离。"""
    plugin = _plugin()
    s1 = plugin.resolve_session(conversation_id="conv1", agent_uid=7,
                                sender_nick="张三", robot_code="rc1", role="admin")
    assert s1.session_id.startswith("dingtalk:7:")
    assert s1.conversation_id == "conv1"
    assert s1.agent_uid == "7"
    assert s1.role == "admin"

    # 同人同会话(不同 conversation_id 亦视为同一单聊 scope)复用同一根
    s2 = plugin.resolve_session(conversation_id="conv1", agent_uid=7,
                                sender_nick="张三", robot_code="rc1")
    assert s2 is s1
    s2b = plugin.resolve_session(conversation_id="convX", agent_uid=7,
                                 sender_nick="张三", robot_code="rc1")
    assert s2b is s1
    assert len(plugin.sessions) == 1
    assert len(plugin._session_by_key) == 1

    # 不同 agent 用户同一会话 → 隔离，互不串上下文
    s4 = plugin.resolve_session(conversation_id="conv1", agent_uid=8,
                                sender_nick="李四", robot_code="rc1")
    assert s4 is not s1
    assert s4.session_id.startswith("dingtalk:8:")
    assert len(plugin.sessions) == 2


def test_resolve_session_group_same_cid_shared_root():
    """群聊同一 cid：不同成员触发 → 同一共享根(整群共享上下文)。"""
    plugin = _plugin()
    cid = "cidGROUP-A=bc"
    s_a = plugin.resolve_session(conversation_id=cid, agent_uid=7,
                                 sender_nick="张三", robot_code="rc1")
    s_b = plugin.resolve_session(conversation_id=cid, agent_uid=8,
                                 sender_nick="李四", robot_code="rc1")
    assert s_a is s_b
    assert s_a.session_id.startswith("dingtalk_group:")
    assert len(plugin.sessions) == 1
    # 行级 conversation_id 保留真实 openConversationId(发图/媒体需要)
    assert s_b.conversation_id == cid

    # 不同群(cid 不同) → 各自独立根, 互不串
    s_c = plugin.resolve_session(conversation_id="cidGROUP-other/xyz==", agent_uid=7,
                                 sender_nick="张三", robot_code="rc1")
    assert s_c is not s_a
    assert s_c.session_id.startswith("dingtalk_group:")
    assert len(plugin.sessions) == 2


def test_resolve_session_force_new():
    """/new(force_new)：单聊换 rand、群换 rand；同一 scope 新根替换旧根。"""
    plugin = _plugin()
    s1 = plugin.resolve_session(conversation_id="conv1", agent_uid=7,
                                sender_nick="张三", robot_code="rc1")
    s2 = plugin.resolve_session(conversation_id="conv1", agent_uid=7,
                                sender_nick="张三", robot_code="rc1", force_new=True)
    assert s2 is not s1
    assert s2.session_id != s1.session_id
    assert s2.session_id.startswith("dingtalk:7:")
    assert plugin._session_by_key[("s", "7")] == s2.session_id

    g1 = plugin.resolve_session(conversation_id="cidG/xx==", agent_uid=7,
                                sender_nick="张三", robot_code="rc1")
    g2 = plugin.resolve_session(conversation_id="cidG/xx==", agent_uid=7,
                                sender_nick="张三", robot_code="rc1", force_new=True)
    assert g2 is not g1
    assert g2.session_id != g1.session_id
    prefix = dingtalk_group_root_prefix("cidG/xx==")
    assert g2.session_id.startswith(prefix)
    assert plugin._session_by_key[("g", prefix)] == g2.session_id


def test_resolve_session_single_continues_recent_root_across_restart(storage):
    """跨重启续根：内存空、DB 有该 uid 最近单聊根 → 复用(不新建)。"""
    with patch("storage.storage.get_storage", return_value=storage):
        storage.save_message_sync("main", "dingtalk:7:oldroot1", "user", "你好",
                                  user_id="dingtalk:7",
                                  conversation_id="dingtalk:7:oldroot1")
        plugin = _plugin()
        s = plugin.resolve_session(conversation_id="", agent_uid=7,
                                   sender_nick="张三", robot_code="rc1")
    assert s.session_id == "dingtalk:7:oldroot1"
    assert len(plugin.sessions) == 1


def test_resolve_session_single_force_new_survives_restart(storage):
    """/new 后跨重启: 走 scope 指针, 不复用 /new 前的旧根。"""
    with patch("storage.storage.get_storage", return_value=storage):
        p1 = _plugin()
        old = p1.resolve_session(conversation_id="", agent_uid=7,
                                 sender_nick="张三", robot_code="rc1")
        new = p1.resolve_session(conversation_id="", agent_uid=7,
                                 sender_nick="张三", robot_code="rc1", force_new=True)
        assert new.session_id != old.session_id
        # 模拟重启: 全新 plugin 实例
        p2 = _plugin()
        s = p2.resolve_session(conversation_id="", agent_uid=7,
                               sender_nick="张三", robot_code="rc1")
    assert s.session_id == new.session_id


def test_resolve_session_group_pointer_across_restart(storage):
    """群根跨重启续根: 靠 scope→根 持久指针(rand 不可推导, 指针必需)。"""
    cid = "cidGROUP-restart/aB=="
    with patch("storage.storage.get_storage", return_value=storage):
        p1 = _plugin()
        g1 = p1.resolve_session(conversation_id=cid, agent_uid=7,
                                sender_nick="张三", robot_code="rc1")
        prefix = dingtalk_group_root_prefix(cid)
        assert storage.get_dingtalk_scope_root("g", prefix) == g1.session_id
        # 模拟重启: 全新 plugin 实例, 同 cid 任意成员触发均复用同根
        p2 = _plugin()
        g2 = p2.resolve_session(conversation_id=cid, agent_uid=9,
                                sender_nick="王五", robot_code="rc1")
    assert g2.session_id == g1.session_id
    assert g2.session_id.startswith("dingtalk_group:")


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


# ---------------- process: 群聊共享根 + /new ----------------

def _bind(storage, name, staff):
    rbac = RBACManager(storage)
    uid = rbac.create_user(name=name, department="技术部", role="admin")
    rbac.bind_identity(uid, "dingtalk", staff)
    return uid


async def test_process_group_same_cid_shared_root_and_group_context(storage):
    """群聊同一 cid：不同成员触发 → 同一共享根，且 route 带 group_context=True。"""
    uid7 = _bind(storage, "张三", "staff-g7")
    uid8 = _bind(storage, "李四", "staff-g8")
    cid = "cidGRPshared+A=="

    router = MagicMock()
    router.route = AsyncMock(return_value=SimpleNamespace(result="已处理"))
    with patch("storage.storage.get_storage", return_value=storage):
        plugin = _plugin()
        handler = _handler(plugin, router=router)
        handler.reply_text = MagicMock()

        await handler.process(SimpleNamespace(data=_msg_data(
            sender_staff_id="staff-g7", sender_nick="张三", conversation_id=cid)))
        await handler.process(SimpleNamespace(data=_msg_data(
            sender_staff_id="staff-g8", sender_nick="李四", conversation_id=cid)))

    assert router.route.await_count == 2
    sids = [c.kwargs["session_id"] for c in router.route.await_args_list]
    assert sids[0] == sids[1]
    assert sids[0].startswith("dingtalk_group:")
    # 两轮路由均带群共享上下文信号；user_id 行级审计保留各触发人
    assert all(c.kwargs["group_context"] is True for c in router.route.await_args_list)
    assert router.route.await_args_list[0].kwargs["user_id"] == f"dingtalk:{uid7}"
    assert router.route.await_args_list[1].kwargs["user_id"] == f"dingtalk:{uid8}"
    # 群共享根仅建一个会话对象
    assert len(plugin.sessions) == 1
    assert plugin.sessions[sids[0]].conversation_id == cid


async def test_process_single_chat_no_group_context(storage):
    """单聊(conversation_id 非 cid 前缀/缺省) 不带 group_context 信号。"""
    _bind(storage, "张三", "staff-s1")
    router = MagicMock()
    router.route = AsyncMock(return_value=SimpleNamespace(result="已处理"))
    with patch("storage.storage.get_storage", return_value=storage):
        plugin = _plugin()
        handler = _handler(plugin, router=router)
        handler.reply_text = MagicMock()
        await handler.process(SimpleNamespace(data=_msg_data(sender_staff_id="staff-s1")))
        # 单聊 conversation_id 缺省
        await handler.process(SimpleNamespace(data=_msg_data(
            sender_staff_id="staff-s1", conversation_id="")))
    assert router.route.await_count == 2
    assert all(c.kwargs.get("group_context") is False or "group_context" not in c.kwargs
               for c in router.route.await_args_list)
    sids = [c.kwargs["session_id"] for c in router.route.await_args_list]
    assert sids[0] == sids[1]  # 单聊同 scope 复用同根
    assert sids[0].startswith("dingtalk:")


async def test_process_new_single(storage):
    """/new(单聊): 回复确认、开新根、不进 agent 路由。"""
    uid = _bind(storage, "张三", "staff-n1")
    router = MagicMock()
    router.route = AsyncMock(return_value=SimpleNamespace(result="不应被路由"))
    with patch("storage.storage.get_storage", return_value=storage):
        plugin = _plugin()
        handler = _handler(plugin, router=router)
        handler.reply_text = MagicMock()
        # 先发一条建立会话
        await handler.process(SimpleNamespace(data=_msg_data(sender_staff_id="staff-n1")))
        sid_before = router.route.await_args.kwargs["session_id"]
        router.route.reset_mock()
        # /new(忽略大小写/首尾空格)
        code, _ = await handler.process(SimpleNamespace(data=_msg_data(
            sender_staff_id="staff-n1", content="  /new  ")))
    assert code == 200
    router.route.assert_not_awaited()
    reply_args, _ = handler.reply_text.call_args
    assert reply_args[0] == "已开启新会话"
    # 内存 scope 指向新根, 且不是旧根
    new_sid = plugin._session_by_key[("s", str(uid))]
    assert new_sid.startswith(f"dingtalk:{uid}:")
    assert new_sid != sid_before


async def test_process_new_group(storage):
    """/new(群聊): 全群开新根并覆写持久指针、回复确认、不进 agent 路由。"""
    _bind(storage, "张三", "staff-ng")
    cid = "cidGRPnew/A=="
    router = MagicMock()
    router.route = AsyncMock(return_value=SimpleNamespace(result="不应被路由"))
    with patch("storage.storage.get_storage", return_value=storage):
        plugin = _plugin()
        handler = _handler(plugin, router=router)
        handler.reply_text = MagicMock()
        await handler.process(SimpleNamespace(data=_msg_data(
            sender_staff_id="staff-ng", conversation_id=cid)))
        old_sid = router.route.await_args.kwargs["session_id"]
        router.route.reset_mock()
        code, _ = await handler.process(SimpleNamespace(data=_msg_data(
            sender_staff_id="staff-ng", conversation_id=cid, content="/NEW")))
    assert code == 200
    router.route.assert_not_awaited()
    prefix = dingtalk_group_root_prefix(cid)
    new_sid = plugin._session_by_key[("g", prefix)]
    assert new_sid.startswith(prefix)
    assert new_sid != old_sid
    # 指针已被覆写为新根(供跨重启续根)
    assert storage.get_dingtalk_scope_root("g", prefix) == new_sid
    reply_args, _ = handler.reply_text.call_args
    assert reply_args[0] == "已开启新会话"
