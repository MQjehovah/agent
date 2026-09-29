"""钉钉群目录(MCP 能力)测试: list_groups / group 名→cid 解析 / 群发工具 / 容错并发。

覆盖:
- _groups_file: env DINGTALK_GROUPS_FILE 优先, 缺省 /app/shared/dingtalk_groups.json;
- _load_groups: 缺失/空/损坏/BOM 容错; 目录路径等 I/O 异常返回空;
- _resolve_conversation_id: cid 直用 / 精确(大小写不敏感) / 模糊唯一 / 模糊多义(candidates)
  / 未命中(hint) / 二者皆空;
- dingtalk_list_groups: keyword 过滤 name/cid、last_active 倒序、空态、limit 校验;
- 群发工具 group→cid: robot/text/markdown(requests) 与 file(_api) 均发出解析后的 cid;
  解析失败不发起请求;
- 并发读写 + 异常容错(读侧永不抛)。
"""
import importlib
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MCP_SRC = ROOT / "mcp_server" / "src"
if str(MCP_SRC) not in sys.path:
    sys.path.insert(0, str(MCP_SRC))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from plugins.dingtalk.groups import upsert_group_entry  # noqa: E402


def _load():
    return importlib.import_module("dingtalk")


def _payload(text):
    return json.loads(text)


def _write_groups(path, groups: dict):
    path.write_text(
        json.dumps({"version": 1, "groups": groups}, ensure_ascii=False),
        encoding="utf-8")


@pytest.fixture
def groups_env(tmp_path, monkeypatch):
    """指向临时群目录文件的 DINGTALK_GROUPS_FILE 环境 + 模块。"""
    module = _load()
    path = tmp_path / "dingtalk_groups.json"
    monkeypatch.setenv("DINGTALK_GROUPS_FILE", str(path))
    return module, path


# ── 路径 / 读取容错 ──

def test_groups_file_env_priority_and_default(monkeypatch):
    module = _load()
    monkeypatch.delenv("DINGTALK_GROUPS_FILE", raising=False)
    assert module._groups_file() == "/app/shared/dingtalk_groups.json"
    monkeypatch.setenv("DINGTALK_GROUPS_FILE", "  /tmp/g.json  ")
    assert module._groups_file() == "/tmp/g.json"
    monkeypatch.setenv("DINGTALK_GROUPS_FILE", " ")
    assert module._groups_file() == "/app/shared/dingtalk_groups.json"


def test_load_groups_tolerates_missing_empty_corrupt_bom(groups_env):
    module, path = groups_env
    # 缺失
    assert module._load_groups() == {}
    # 空
    path.write_text("", encoding="utf-8")
    assert module._load_groups() == {}
    # 损坏
    path.write_text('{"groups": [1,2]}', encoding="utf-8")
    assert module._load_groups() == {}
    # BOM + 合法条目
    path.write_text(
        json.dumps({"version": 1, "groups": {"cid1": {"name": "群A"}}}),
        encoding="utf-8-sig")
    assert module._load_groups()["cid1"]["name"] == "群A"


def test_load_groups_io_exception_returns_empty(groups_env, tmp_path, monkeypatch):
    module, _ = groups_env
    monkeypatch.setenv("DINGTALK_GROUPS_FILE", str(tmp_path))  # 目录: open 必失败
    assert module._load_groups() == {}


# ── 纯函数: _resolve_conversation_id ──

def test_resolve_conversation_id_direct_wins():
    module = _load()
    cid, error = module._resolve_conversation_id("cidDirect", "不存在群", {"cid1": {"name": "群A"}})
    assert cid == "cidDirect"
    assert error is None


def test_resolve_conversation_id_exact_case_insensitive():
    module = _load()
    groups = {
        "cid1": {"name": "设备运维群", "last_active": "2026-09-29T10:00:00+08:00"},
        "cid2": {"name": "设备运维群（旧）", "last_active": "2026-09-29T11:00:00+08:00"},
    }
    cid, error = module._resolve_conversation_id("", "设备运维群", groups)
    assert error is None
    assert cid == "cid1"  # 精确命中而非模糊


def test_resolve_exact_multiple_same_name_picks_latest():
    module = _load()
    groups = {
        "cidOld": {"name": "同名群", "last_active": "2026-09-01T09:00:00+08:00"},
        "cidNew": {"name": "同名群", "last_active": "2026-09-29T09:00:00+08:00"},
    }
    cid, error = module._resolve_conversation_id("", "同名群", groups)
    assert error is None
    assert cid == "cidNew"


def test_resolve_conversation_id_fuzzy_unique():
    module = _load()
    groups = {
        "cid1": {"name": "设备运维群", "last_active": "2026-09-29T10:00:00+08:00"},
        "cid2": {"name": "IT运维群", "last_active": "2026-09-29T11:00:00+08:00"},
    }
    cid, error = module._resolve_conversation_id("", "设备运维", groups)
    assert error is None
    assert cid == "cid1"


def test_resolve_conversation_id_fuzzy_ambiguous_returns_candidates():
    module = _load()
    groups = {
        "cid1": {"name": "设备运维群", "last_active": "2026-09-29T10:00:00+08:00"},
        "cid2": {"name": "设备运维通知群", "last_active": "2026-09-29T11:00:00+08:00"},
        "cid3": {"name": "IT运维群", "last_active": "2026-09-29T12:00:00+08:00"},
    }
    cid, error = module._resolve_conversation_id("", "设备运维", groups)
    assert cid == ""
    assert error is not None
    assert "多个群" in error["error"]
    candidates = error["candidates"]
    assert {item["conversation_id"] for item in candidates} == {"cid1", "cid2"}
    # 候选按 last_active 倒序
    assert candidates[0]["conversation_id"] == "cid2"
    assert error["hint"]


def test_resolve_conversation_id_not_found_and_empty(groups_env):
    module, _ = groups_env
    cid, error = module._resolve_conversation_id("", "神秘群", {"cid1": {"name": "群A"}})
    assert cid == ""
    assert "未找到群" in error["error"]
    assert "群A" in error["hint"]

    cid, error = module._resolve_conversation_id("", "", {})
    assert cid == ""
    assert error is not None
    assert "至少提供一个" in error["error"]
    assert error["hint"]


# ── 工具: dingtalk_list_groups ──

def test_list_groups_filter_sort_and_empty(groups_env):
    module, path = groups_env
    # 空态(文件缺失)
    payload = _payload(module.dingtalk_list_groups())
    assert payload == {"success": True, "count": 0, "groups": []}

    _write_groups(path, {
        "cidIT": {"name": "IT运维群", "robot_code": "rc-it", "sender": "李四",
                  "last_active": "2026-09-29T09:00:00+08:00"},
        "cidDev": {"name": "设备运维群", "robot_code": "rc-dev", "sender": "张三",
                   "last_active": "2026-09-29T12:00:00+08:00"},
        "cidTest": {"name": "测试群", "robot_code": "", "sender": "",
                    "last_active": "2026-09-28T08:00:00+08:00"},
    })
    payload = _payload(module.dingtalk_list_groups())
    assert payload["success"] is True
    assert payload["count"] == 3
    assert [row["conversation_id"] for row in payload["groups"]] == ["cidDev", "cidIT", "cidTest"]
    assert payload["groups"][0] == {
        "name": "设备运维群", "conversation_id": "cidDev", "robot_code": "rc-dev",
        "sender": "张三", "last_active": "2026-09-29T12:00:00+08:00"}

    # keyword 过滤 name(不区分大小写)
    payload = _payload(module.dingtalk_list_groups(keyword="运维"))
    assert {row["conversation_id"] for row in payload["groups"]} == {"cidIT", "cidDev"}
    # keyword 过滤 cid
    payload = _payload(module.dingtalk_list_groups(keyword="cidtest"))
    assert payload["count"] == 1
    assert payload["groups"][0]["name"] == "测试群"
    # limit 截断
    payload = _payload(module.dingtalk_list_groups(limit=1))
    assert payload["count"] == 1
    assert payload["groups"][0]["conversation_id"] == "cidDev"
    # limit 非法
    payload = _payload(module.dingtalk_list_groups(limit=0))
    assert payload["success"] is False
    assert "limit" in payload["error"]


# ── 群发工具: group → cid ──

class _FakeResponse:
    status_code = 200

    def __init__(self, body):
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


@pytest.fixture
def send_env(groups_env, monkeypatch):
    """在 groups_env 基础上补桩: 凭证/token/requests.post + 群目录数据。"""
    module, path = groups_env
    _write_groups(path, {
        "cidDev": {"name": "设备运维群", "robot_code": "rc-dev", "sender": "张三",
                   "last_active": "2026-09-29T12:00:00+08:00"},
        "cidIT": {"name": "IT运维群", "robot_code": "rc-it", "sender": "李四",
                  "last_active": "2026-09-29T09:00:00+08:00"},
    })
    monkeypatch.setattr(module, "APP_KEY", "test-key")
    monkeypatch.setattr(module, "APP_SECRET", "test-secret")
    monkeypatch.setattr(module, "_get_access_token", lambda: "tok")
    posts = []

    def fake_post(url, **kwargs):
        posts.append({"url": url, **kwargs})
        return _FakeResponse({"processQueryKey": "PQ-1"})

    monkeypatch.setattr(module.requests, "post", fake_post)
    return module, path, posts


def test_robot_group_by_name_resolves_cid(send_env):
    module, _, posts = send_env
    payload = _payload(module.dingtalk_send_robot_group_message(
        msg_key="sampleText", msg_param='{"content":"hi"}', group="设备运维群"))
    assert payload["success"] is True
    assert len(posts) == 1
    assert posts[0]["json"]["conversationId"] == "cidDev"
    assert posts[0]["url"].endswith("/v1.0/robot/groupMessages/send")


def test_text_and_markdown_group_by_name_resolve_cid(send_env):
    module, _, posts = send_env
    payload = _payload(module.dingtalk_send_text_group(content="你好", group="it运维群"))
    assert payload["success"] is True
    assert posts[0]["json"]["conversationId"] == "cidIT"
    assert posts[0]["json"]["msgKey"] == "sampleText"

    # 精确未命中 → 模糊唯一("设备运维" 仅被一个群名包含)
    payload = _payload(module.dingtalk_send_markdown_group(
        title="标题", text="# 正文", group="设备运维"))
    assert payload["success"] is True
    assert posts[1]["json"]["conversationId"] == "cidDev"
    assert posts[1]["json"]["msgKey"] == "sampleMarkdown"

    # 模糊多义("运维" 同时命中两个群) → 报错且不发起请求
    payload = _payload(module.dingtalk_send_text_group(content="x", group="运维"))
    assert payload["success"] is False
    assert payload["candidates"]
    assert len(posts) == 2


def test_group_send_resolution_failure_no_http(send_env):
    module, _, posts = send_env
    payload = _payload(module.dingtalk_send_robot_group_message(
        msg_key="sampleText", msg_param="{}", group="神秘群"))
    assert payload["success"] is False
    assert "未找到群" in payload["error"]
    assert payload["hint"]

    payload = _payload(module.dingtalk_send_robot_group_message(
        msg_key="sampleText", msg_param="{}"))
    assert payload["success"] is False
    assert "至少提供一个" in payload["error"]
    assert posts == []


def test_file_group_by_name_resolves_cid(send_env, monkeypatch, tmp_path):
    module, _, _ = send_env
    local = tmp_path / "report.pdf"
    local.write_bytes(b"pdf")
    calls = []

    def fake_api(method, path, *, params=None, json_body=None, data=None,
                 files=None, timeout=10):
        calls.append({"method": method, "path": path, "json": json_body})
        if "upload" in path:
            return {"mediaId": "M-1"}
        return {"processQueryKey": "PQ-2"}

    monkeypatch.setattr(module, "_api", fake_api)
    payload = _payload(module.dingtalk_send_file_group(group="设备运维群", file_path=str(local)))
    assert payload["success"] is True
    assert payload["process_query_key"] == "PQ-2"
    assert calls[1]["json"]["conversationId"] == "cidDev"
    assert calls[1]["json"]["msgKey"] == "sampleFile"

    # 解析失败: 不触达上传/发送
    calls.clear()
    payload = _payload(module.dingtalk_send_file_group(group="神秘群", file_path=str(local)))
    assert payload["success"] is False
    assert calls == []


# ── 并发 / 异常容错 ──

def test_load_groups_concurrent_with_atomic_writer(groups_env):
    """读侧并发读取 + 写侧原子 upsert: 读永不抛, 已登记 cid 始终可见。"""
    module, path = groups_env
    upsert_group_entry(str(path), "cidBase", "基础群", sender="张三")
    errors = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                data = module._load_groups()
                assert isinstance(data, dict)
                if data:
                    assert "cidBase" in data
        except Exception as e:  # pragma: no cover - 记录后断言失败
            errors.append(e)

    def writer():
        try:
            for i in range(50):
                upsert_group_entry(str(path), f"cidW{i}", f"并发群{i}", sender="李四")
        except Exception as e:  # pragma: no cover
            errors.append(e)
        finally:
            stop.set()

    threads = [threading.Thread(target=reader) for _ in range(4)]
    threads.append(threading.Thread(target=writer))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert "cidBase" in module._load_groups()
