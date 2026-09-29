"""钉钉群目录(插件侧)测试: groups.py 纯逻辑 + 插件收到群消息后回写。

覆盖: upsert 新建/更新/同名不同 cid/剪枝上限/损坏与 BOM 容错/原子替换无 tmp 残留/
失败仅告警; 插件级: 群消息写入 (cid, 群名, robot_code, 最近触发人), 单聊与非 cid 不写。
"""
import json
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from plugins.dingtalk import NOT_PROVISIONED_REPLY, AgentChatbotHandler, DingTalkPlugin
from plugins.dingtalk.groups import (
    DEFAULT_GROUPS_FILE,
    MAX_GROUPS,
    groups_file_path,
    load_groups,
    upsert_group_entry,
)
from security.rbac import RBACManager
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
        self.conversation_type = d.get("conversation_type", "")
        self.conversation_title = d.get("conversation_title", "")
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
    """把 dingtalk_stream 换成桩模块, 覆盖插件内的惰性 import。"""
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


def _write_raw(path, text: str):
    path.write_text(text, encoding="utf-8")


def _read_groups(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["groups"]


def _no_tmp_files(path) -> bool:
    directory = os.path.dirname(os.path.abspath(str(path)))
    return not [name for name in os.listdir(directory) if name.endswith(".tmp")]


# ---------------- groups.py 纯逻辑 ----------------

def test_groups_file_path_default_and_env(monkeypatch):
    monkeypatch.delenv("DINGTALK_GROUPS_FILE", raising=False)
    assert groups_file_path() == DEFAULT_GROUPS_FILE
    monkeypatch.setenv("DINGTALK_GROUPS_FILE", "  /tmp/x.json  ")
    assert groups_file_path() == "/tmp/x.json"
    monkeypatch.setenv("DINGTALK_GROUPS_FILE", "   ")
    assert groups_file_path() == DEFAULT_GROUPS_FILE


def test_load_groups_missing_empty_corrupt_and_bom(tmp_path, monkeypatch):
    path = tmp_path / "groups.json"
    # 缺失
    assert load_groups(str(path)) == {"version": 1, "groups": {}}
    # 空文件
    path.write_text("", encoding="utf-8")
    assert load_groups(str(path))["groups"] == {}
    # 损坏 JSON
    path.write_text("{not json", encoding="utf-8")
    assert load_groups(str(path))["groups"] == {}
    # 结构不对
    path.write_text('["a"]', encoding="utf-8")
    assert load_groups(str(path))["groups"] == {}
    # BOM 容错(utf-8-sig 写出)
    path.write_text(
        json.dumps({"version": 1, "groups": {"cidBOM": {"name": "带BOM"}}}),
        encoding="utf-8-sig")
    data = load_groups(str(path))
    assert data["groups"]["cidBOM"]["name"] == "带BOM"
    # 缺省路径走 groups_file_path()
    monkeypatch.setenv("DINGTALK_GROUPS_FILE", str(path))
    assert load_groups()["groups"]["cidBOM"]["name"] == "带BOM"


def test_upsert_creates_and_updates_entry(tmp_path):
    path = tmp_path / "groups.json"
    assert upsert_group_entry(str(path), "cidA", "设备运维群", "rc-1", "张三") is True
    groups = _read_groups(path)
    entry = groups["cidA"]
    assert entry["name"] == "设备运维群"
    assert entry["robot_code"] == "rc-1"
    assert entry["sender"] == "张三"
    assert entry["last_active"].startswith("20")
    assert "+08:00" in entry["last_active"]

    first_active = entry["last_active"]
    assert upsert_group_entry(str(path), "cidA", "设备运维群", "rc-9", "李四") is True
    groups = _read_groups(path)
    assert len(groups) == 1
    assert groups["cidA"]["robot_code"] == "rc-9"
    assert groups["cidA"]["sender"] == "李四"
    assert groups["cidA"]["last_active"] >= first_active
    assert _no_tmp_files(path)


def test_upsert_same_name_different_cids(tmp_path):
    path = tmp_path / "groups.json"
    assert upsert_group_entry(str(path), "cidOne", "同名群", sender="张三") is True
    assert upsert_group_entry(str(path), "cidTwo", "同名群", sender="李四") is True
    groups = _read_groups(path)
    assert set(groups) == {"cidOne", "cidTwo"}
    assert groups["cidOne"]["name"] == groups["cidTwo"]["name"] == "同名群"


def test_upsert_empty_name_keeps_existing(tmp_path):
    path = tmp_path / "groups.json"
    upsert_group_entry(str(path), "cidA", "原群名", sender="张三")
    upsert_group_entry(str(path), "cidA", "", sender="李四")
    assert _read_groups(path)["cidA"]["name"] == "原群名"


def test_upsert_prunes_to_latest_max_groups(tmp_path):
    path = tmp_path / "groups.json"
    groups = {
        f"cidOld{i:03d}": {"name": f"旧群{i}", "last_active": f"2020-01-01T00:00:{i % 60:02d}+08:00"}
        for i in range(MAX_GROUPS)
    }
    path.write_text(json.dumps({"version": 1, "groups": groups}, ensure_ascii=False),
                    encoding="utf-8")
    assert upsert_group_entry(str(path), "cidNewest", "最新群", sender="张三") is True
    saved = _read_groups(path)
    assert len(saved) == MAX_GROUPS
    assert "cidNewest" in saved
    # 既有 200 个中应被挤掉 1 个最旧的
    dropped = set(groups) - set(saved)
    assert len(dropped) == 1
    assert all(entry["last_active"] < saved["cidNewest"]["last_active"]
               for cid, entry in saved.items() if cid != "cidNewest")


def test_upsert_corrupt_file_is_recovered(tmp_path):
    path = tmp_path / "groups.json"
    _write_raw(path, "\x00broken{{")
    assert upsert_group_entry(str(path), "cidA", "群A") is True
    groups = _read_groups(path)
    assert list(groups) == ["cidA"]


def test_upsert_empty_cid_rejected(tmp_path):
    path = tmp_path / "groups.json"
    assert upsert_group_entry(str(path), "", "群A") is False
    assert not path.exists()


def test_upsert_failure_warns_and_returns_false(tmp_path, caplog, monkeypatch):
    path = tmp_path / "groups.json"
    import plugins.dingtalk.groups as groups_mod

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(groups_mod, "_atomic_write", _boom)
    with caplog.at_level("WARNING", logger="plugin.dingtalk.groups"):
        assert upsert_group_entry(str(path), "cidA", "群A") is False
    assert any("回写钉钉群目录失败" in record.message for record in caplog.records)


# ---------------- 插件级: 收到群消息回写 ----------------

def _plugin():
    return DingTalkPlugin(config_path="/nonexistent/dingtalk.json")


def _bind(storage, name, staff):
    rbac = RBACManager(storage)
    uid = rbac.create_user(name=name, department="技术部", role="admin")
    rbac.bind_identity(uid, "dingtalk", staff)
    return uid


def _handler(plugin, router=None):
    handler = AgentChatbotHandler(plugin)
    plugin.plugin_manager = MagicMock()
    plugin.plugin_manager.router = router
    handler.reply_text = MagicMock()
    return handler


async def test_group_message_writes_group_directory(tmp_path, monkeypatch, storage):
    path = tmp_path / "shared" / "dingtalk_groups.json"
    monkeypatch.setenv("DINGTALK_GROUPS_FILE", str(path))
    uid = _bind(storage, "张三", "staff-g1")
    router = MagicMock()
    router.route = AsyncMock(return_value=SimpleNamespace(result="已处理"))
    with patch("storage.storage.get_storage", return_value=storage):
        plugin = _plugin()
        handler = _handler(plugin, router=router)
        code, _ = await handler.process(SimpleNamespace(data={
            "sender_id": "s1", "sender_staff_id": "staff-g1", "sender_nick": "张三",
            "conversation_id": "cidGRPdir+A==", "conversation_type": "2",
            "conversation_title": "设备运维群", "robot_code": "rc-1", "content": "你好",
        }))
    assert code == 200
    assert router.route.await_count == 1
    groups = _read_groups(path)
    entry = groups["cidGRPdir+A=="]
    assert entry["name"] == "设备运维群"
    assert entry["robot_code"] == "rc-1"
    assert entry["sender"] == "张三"
    assert entry["last_active"]
    assert router.route.await_args.kwargs["user_id"] == f"dingtalk:{uid}"
    assert _no_tmp_files(path)


async def test_group_message_without_title_still_registers(tmp_path, monkeypatch, storage):
    """SDK 缺 conversation_title 时不得报错, cid 仍登记(群名为空串)。"""
    path = tmp_path / "dingtalk_groups.json"
    monkeypatch.setenv("DINGTALK_GROUPS_FILE", str(path))
    _bind(storage, "张三", "staff-g2")
    router = MagicMock()
    router.route = AsyncMock(return_value=SimpleNamespace(result="已处理"))
    with patch("storage.storage.get_storage", return_value=storage):
        plugin = _plugin()
        handler = _handler(plugin, router=router)
        code, _ = await handler.process(SimpleNamespace(data={
            "sender_staff_id": "staff-g2", "sender_nick": "张三",
            "conversation_id": "cidNoTitle", "conversation_type": "2", "content": "你好",
        }))
    assert code == 200
    assert _read_groups(path)["cidNoTitle"]["name"] == ""


async def test_unprovisioned_group_message_still_registers(tmp_path, monkeypatch, storage):
    """未开户成员群内发言: 仍登记群目录(sender 取 sender_nick), 拒绝路由路径不变。"""
    path = tmp_path / "dingtalk_groups.json"
    monkeypatch.setenv("DINGTALK_GROUPS_FILE", str(path))
    router = MagicMock()
    router.route = AsyncMock(return_value=SimpleNamespace(result="不应被路由"))
    with patch("storage.storage.get_storage", return_value=storage):
        plugin = _plugin()
        handler = _handler(plugin, router=router)
        code, _ = await handler.process(SimpleNamespace(data={
            "sender_id": "s-unbound", "sender_staff_id": "staff-unbound",
            "sender_nick": "路人甲", "conversation_id": "cidGRPunbound==",
            "conversation_type": "2", "conversation_title": "吃瓜群",
            "robot_code": "rc-1", "content": "你好",
        }))
    assert code == 200
    # 未开户: 不路由、不建会话, 回复未开户提示不变
    router.route.assert_not_awaited()
    assert plugin.sessions == {}
    args, kwargs = handler.reply_text.call_args
    assert args[0] == NOT_PROVISIONED_REPLY
    assert kwargs.get("msgtype") == "text"
    # 但群已被登记(sender=发送者昵称, 与发送者身份无关)
    entry = _read_groups(path)["cidGRPunbound=="]
    assert entry["name"] == "吃瓜群"
    assert entry["robot_code"] == "rc-1"
    assert entry["sender"] == "路人甲"


async def test_single_chat_and_non_cid_do_not_write(tmp_path, monkeypatch, storage):
    path = tmp_path / "dingtalk_groups.json"
    monkeypatch.setenv("DINGTALK_GROUPS_FILE", str(path))
    _bind(storage, "张三", "staff-single")
    router = MagicMock()
    router.route = AsyncMock(return_value=SimpleNamespace(result="已处理"))
    with patch("storage.storage.get_storage", return_value=storage):
        plugin = _plugin()
        handler = _handler(plugin, router=router)
        # 单聊: conversation_type='1', 即使 cid 开头也不属于群消息分支
        await handler.process(SimpleNamespace(data={
            "sender_staff_id": "staff-single", "sender_nick": "张三",
            "conversation_id": "cidLooksLikeGroup", "conversation_type": "1",
            "conversation_title": "不是群", "content": "你好",
        }))
        # 群判定为真但 cid 前缀不符: 不记录
        await handler.process(SimpleNamespace(data={
            "sender_staff_id": "staff-single", "sender_nick": "张三",
            "conversation_id": "conv-not-cid", "conversation_type": "2",
            "conversation_title": "无前缀群", "content": "你好",
        }))
    assert router.route.await_count == 2
    assert not path.exists()
