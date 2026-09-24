"""钉钉 MCP unionId 自动换算测试。

覆盖:
- 纯数字 userId → 调 topapi/v2/user/get 换算 unionId 并用于 v1.0 路径; 进程内缓存;
- 非数字视为 unionId 直接用(不查询);
- 换算失败/无 unionid → 明确错误且不发 v1.0 请求;
- 六个 unionId 类工具(待办 3 + 日程 3)与 todo_create.creator_id 接入换算;
- dingtalk_get_user_detail 返回 unionid。
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MCP_SRC = ROOT / "mcp_server" / "src"
if str(MCP_SRC) not in sys.path:
    sys.path.insert(0, str(MCP_SRC))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _load():
    return importlib.import_module("dingtalk")


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if isinstance(body, dict) else {}
        self.text = json.dumps(self._body, ensure_ascii=False) if body is not None else ""

    def json(self):
        return self._body


@pytest.fixture
def env(monkeypatch):
    """假 requests.post(topapi user/get) + 假 _api, 记录调用; 清换算/索引缓存。"""
    module = _load()
    module._UNIONID_CACHE.clear()
    module._USERID_CACHE.clear()
    module._directory_index_cache["data"] = None
    module._directory_index_cache["built_at"] = 0.0
    monkeypatch.setattr(module, "APP_KEY", "test-key")
    monkeypatch.setattr(module, "APP_SECRET", "test-secret")
    monkeypatch.setattr(module, "_get_access_token", lambda: "tok")
    state = {
        "api": [],
        "user_get": [],
        "user_get_response": FakeResponse(200, {"errcode": 0, "result": {"unionid": "UNION-1"}}),
    }

    def fake_post(url, json=None, timeout=10):
        state["user_get"].append({"url": url, "json": json})
        response = state["user_get_response"]
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(module.requests, "post", fake_post)

    def fake_api(method, path, *, params=None, json_body=None, **kwargs):
        state["api"].append({"method": method, "path": path, "json": json_body, "params": params})
        return {}

    monkeypatch.setattr(module, "_api", fake_api)
    return module, state


# ===== 1. 换算与缓存 =====

def test_todo_create_numeric_id_resolves_unionid(env):
    module, state = env
    payload = json.loads(module.dingtalk_todo_create(
        dingtalk_unionid="1642483198771392", subject="写周报", dingtalk_userids="u1"))
    assert payload["success"] is True
    assert state["api"][0]["path"] == "/v1.0/todo/users/UNION-1/tasks"
    assert state["user_get"][0]["url"].startswith(
        "https://oapi.dingtalk.com/topapi/v2/user/get?access_token=tok")
    assert state["user_get"][0]["json"] == {"userid": "1642483198771392"}


def test_unionid_cache_avoids_repeat_query(env):
    module, state = env
    module.dingtalk_todo_create(dingtalk_unionid="1642483198771392", subject="a", dingtalk_userids="u1")
    module.dingtalk_todo_create(dingtalk_unionid="1642483198771392", subject="b", dingtalk_userids="u1")
    assert len(state["user_get"]) == 1
    assert [c["path"] for c in state["api"]] == ["/v1.0/todo/users/UNION-1/tasks"] * 2


def test_non_numeric_union_id_used_directly(env):
    module, state = env
    payload = json.loads(module.dingtalk_todo_list(dingtalk_unionid="uni-direct"))
    assert payload["success"] is True
    assert state["user_get"] == []
    assert state["api"][0]["path"] == "/v1.0/todo/users/uni-direct/tasks/list"


def test_resolve_unionid_empty_value(env):
    module, _ = env
    assert module._resolve_unionid("") == ("", "unionId/userId 不能为空")


# ===== 2. 失败路径: 明确错误且不发 v1.0 请求 =====

def test_numeric_lookup_errcode_returns_error_without_v1_call(env):
    module, state = env
    state["user_get_response"] = FakeResponse(200, {"errcode": 60121, "errmsg": "找不到该用户"})
    payload = json.loads(module.dingtalk_todo_create(
        dingtalk_unionid="1642483198771392", subject="s", dingtalk_userids="u1"))
    assert payload["success"] is False
    assert "未找到该钉钉用户(userId=1642483198771392)" in payload["error"]
    assert state["api"] == []


def test_numeric_lookup_missing_unionid_returns_error_without_v1_call(env):
    module, state = env
    state["user_get_response"] = FakeResponse(200, {"errcode": 0, "result": {"name": "x"}})
    payload = json.loads(module.dingtalk_todo_list(dingtalk_unionid="1642483198771392"))
    assert payload["success"] is False
    assert "未找到该钉钉用户(userId=1642483198771392)" in payload["error"]
    assert state["api"] == []


def test_numeric_lookup_request_exception_returns_error(env):
    module, state = env
    state["user_get_response"] = RuntimeError("connection reset")
    payload = json.loads(module.dingtalk_calendar_create_event(
        dingtalk_unionid="1642483198771392", summary="s", start_time="a", end_time="b"))
    assert payload["success"] is False
    assert "换算 unionId 失败" in payload["error"]
    assert "connection reset" in payload["error"]
    assert state["api"] == []


# ===== 3. 六个工具 + creator_id 接入 =====

def test_six_union_tools_resolve_numeric_user_id(env):
    module, state = env
    uid = "1642483198771392"
    module.dingtalk_todo_create(dingtalk_unionid=uid, subject="s", dingtalk_userids="u1")
    module.dingtalk_todo_update(dingtalk_unionid=uid, task_id="T1", done=True)
    module.dingtalk_todo_list(dingtalk_unionid=uid)
    module.dingtalk_calendar_create_event(dingtalk_unionid=uid, summary="s",
                                          start_time="2026-09-24T10:00:00+08:00",
                                          end_time="2026-09-24T11:00:00+08:00")
    module.dingtalk_calendar_list_events(dingtalk_unionid=uid)
    module.dingtalk_calendar_freebusy(dingtalk_unionid=uid, dingtalk_unionids="UNION-2",
                                      start_time="a", end_time="b")
    assert [c["path"] for c in state["api"]] == [
        "/v1.0/todo/users/UNION-1/tasks",
        "/v1.0/todo/users/UNION-1/tasks/T1",
        "/v1.0/todo/users/UNION-1/tasks/list",
        "/v1.0/calendar/users/UNION-1/calendars/primary/events",
        "/v1.0/calendar/users/UNION-1/calendars/primary/events",
        "/v1.0/calendar/users/UNION-1/querySchedule",
    ]
    assert len(state["user_get"]) == 1  # 缓存: 六次调用只查一次


def test_todo_create_creator_id_numeric_resolved(env):
    module, state = env
    payload = json.loads(module.dingtalk_todo_create(
        dingtalk_unionid="uni-owner", subject="s", dingtalk_userids="u1",
        dingtalk_creator_unionid="1642483198771392"))
    assert payload["success"] is True
    assert state["api"][0]["json"]["creatorId"] == "UNION-1"
    assert len(state["user_get"]) == 1


# ===== 4. 用户详情返回 unionid =====

def test_get_user_detail_includes_unionid(env):
    module, state = env
    state["user_get_response"] = FakeResponse(200, {"errcode": 0, "result": {
        "userid": "1642483198771392", "name": "季明清", "unionid": "UNION-1"}})
    payload = json.loads(module.dingtalk_get_user_detail("1642483198771392"))
    assert payload["success"] is True
    assert payload["user"]["userid"] == "1642483198771392"
    assert payload["user"]["unionid"] == "UNION-1"
