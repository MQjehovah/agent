"""钉钉连接器(MCP)办公 API 扩展测试: 审批/待办/日程。

用 monkeypatch 替换模块内 ``_api`` 注入假 HTTP: 每个新工具覆盖成功路径与
参数必填/非法路径(参数错误不得触达 HTTP), 并断言工具注解(查询 read / 普通写
非破坏 / 审批同意拒绝 destructive)。
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
# conftest 的 _init_settings 依赖 agent 的 src 在 sys.path 上(其它测试文件亦如此);
# 本文件可独立运行, 故自行补齐。
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _load():
    return importlib.import_module("dingtalk")


def _payload(text: str):
    return json.loads(text)


@pytest.fixture
def api(monkeypatch):
    """假 _api: 记录调用并按 path 关键字返回预设响应。"""
    module = _load()
    monkeypatch.setattr(module, "APP_KEY", "test-key")
    monkeypatch.setattr(module, "APP_SECRET", "test-secret")
    calls = []
    responses = {}

    def fake_api(method, path, *, params=None, json_body=None, timeout=10):
        calls.append({"method": method, "path": path, "params": params, "json": json_body})
        for key, value in responses.items():
            if key in path:
                if isinstance(value, Exception):
                    raise value
                return value
        return {}

    monkeypatch.setattr(module, "_api", fake_api)
    return module, calls, responses


# ── 审批 ──

def test_approval_start_success(api):
    module, calls, responses = api
    responses["/v1.0/workflow/processInstances"] = {"instanceId": "PI-123"}
    payload = _payload(module.dingtalk_approval_start(
        process_code="PROC-1", originator_user_id="user1",
        form_values='[{"name":"金额","value":"100"}]',
        dept_id=42, cc_list="user2,user3"))
    assert payload == {"success": True, "process_instance_id": "PI-123"}
    call = calls[0]
    assert call["method"] == "POST"
    assert call["json"]["processCode"] == "PROC-1"
    assert call["json"]["originatorUserId"] == "user1"
    assert call["json"]["deptId"] == 42
    assert call["json"]["ccList"] == ["user2", "user3"]
    assert call["json"]["formComponentValues"] == [{"name": "金额", "value": "100"}]


def test_approval_start_param_errors_do_not_call_http(api):
    module, calls, _ = api
    for kwargs in (
        {"process_code": "", "originator_user_id": "u1"},
        {"process_code": "P1", "originator_user_id": ""},
        {"process_code": "P1", "originator_user_id": "u1", "form_values": "not-json"},
        {"process_code": "P1", "originator_user_id": "u1", "form_values": '{"name":"x"}'},
        {"process_code": "P1", "originator_user_id": "u1", "form_values": '[{"value":"x"}]'},
        {"process_code": "P1", "originator_user_id": "u1", "approvers": "bad-json"},
    ):
        payload = _payload(module.dingtalk_approval_start(**kwargs))
        assert payload["success"] is False
        assert payload["error"]
    assert calls == []


def test_approval_instance_success(api):
    module, calls, responses = api
    responses["/v1.0/workflow/processInstances"] = {
        "success": "true",
        "result": {
            "title": "报销申请", "status": "RUNNING", "result": "agree",
            "originatorUserId": "user1", "createTime": "2026-09-24T10:00Z",
            "tasks": [{"taskId": 9, "activityId": "a1", "status": "RUNNING", "result": "NONE"}],
            "formComponentValues": [{"name": "金额", "value": "100"}],
        },
    }
    payload = _payload(module.dingtalk_approval_instance("PI-1"))
    assert payload["success"] is True
    instance = payload["instance"]
    assert instance["process_instance_id"] == "PI-1"
    assert instance["title"] == "报销申请"
    assert instance["tasks"][0]["task_id"] == 9
    assert instance["form_values"][0]["name"] == "金额"
    assert calls[0]["method"] == "GET"
    assert calls[0]["params"] == {"processInstanceId": "PI-1"}


def test_approval_instance_requires_id(api):
    module, calls, _ = api
    payload = _payload(module.dingtalk_approval_instance(""))
    assert payload["success"] is False
    assert "process_instance_id" in payload["error"]
    assert calls == []


def test_approval_tasks_success_and_validation(api):
    module, calls, responses = api
    responses["/v1.0/workflow/workRecords/todoTasks"] = {
        "result": {"list": [{"taskId": 7, "instanceId": "PI-2", "title": "请假审批",
                             "url": "https://x", "forms": [{"title": "天数", "content": "2"}]}],
                   "nextToken": 5},
    }
    payload = _payload(module.dingtalk_approval_tasks("user1", status=1, max_results=500))
    assert payload["success"] is True
    assert payload["tasks"][0]["task_id"] == 7
    assert payload["tasks"][0]["forms"][0]["title"] == "天数"
    assert payload["next_token"] == 5
    assert calls[0]["params"]["status"] == 1
    assert calls[0]["params"]["maxResults"] == 100   # 上限 100

    for kwargs in ({"user_id": ""}, {"user_id": "u1", "status": 2}, {"user_id": "u1", "status": True}):
        payload = _payload(module.dingtalk_approval_tasks(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_approval_action_success_and_aliases(api):
    module, calls, responses = api
    responses["/v1.0/workflow/processInstances/execute"] = {"success": True, "result": True}
    payload = _payload(module.dingtalk_approval_action(
        task_id=9, result="同意", remark="同意报销", process_instance_id="PI-1", actioner_user_id="user1"))
    assert payload["success"] is True
    assert calls[0]["json"] == {"taskId": 9, "result": "agree", "remark": "同意报销",
                                "processInstanceId": "PI-1", "actionerUserId": "user1"}

    payload = _payload(module.dingtalk_approval_action(task_id=9, result="refuse"))
    assert payload["success"] is True
    assert calls[1]["json"]["result"] == "refuse"

    responses["/v1.0/workflow/processInstances/execute"] = {"success": False, "result": False}
    payload = _payload(module.dingtalk_approval_action(task_id=9, result="agree"))
    assert payload["success"] is False


def test_approval_action_param_errors_do_not_call_http(api):
    module, calls, _ = api
    for kwargs in (
        {"task_id": None, "result": "agree"},
        {"task_id": 9, "result": "maybe"},
        {"task_id": 9, "result": ""},
    ):
        payload = _payload(module.dingtalk_approval_action(**kwargs))
        assert payload["success"] is False
    assert calls == []


# ── 待办 ──

def test_todo_create_success_and_validation(api):
    module, calls, responses = api
    responses["/tasks"] = {"id": "TODO-1"}
    payload = _payload(module.dingtalk_todo_create(
        union_id="uni1", subject="写周报", executor_ids="user1,user2",
        description="周五前", due_time_ms=1750000000000, priority=20,
        detail_url="https://x", source_id="src-1"))
    assert payload == {"success": True, "task_id": "TODO-1"}
    body = calls[0]["json"]
    assert body["subject"] == "写周报"
    assert body["executorIds"] == ["user1", "user2"]
    assert body["dueTime"] == 1750000000000
    assert body["detailUrl"] == {"pcUrl": "https://x", "appUrl": "https://x"}
    assert body["sourceId"] == "src-1"

    for kwargs in (
        {"union_id": "", "subject": "s", "executor_ids": "u1"},
        {"union_id": "uni1", "subject": "", "executor_ids": "u1"},
        {"union_id": "uni1", "subject": "s", "executor_ids": ""},
    ):
        payload = _payload(module.dingtalk_todo_create(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_todo_update_success_and_validation(api):
    module, calls, responses = api
    responses["/tasks/TODO-1"] = {"result": True}
    payload = _payload(module.dingtalk_todo_update(
        union_id="uni1", task_id="TODO-1", done=True, description="已完成"))
    assert payload["success"] is True
    assert calls[0]["method"] == "PUT"
    assert calls[0]["json"] == {"done": True, "description": "已完成"}

    payload = _payload(module.dingtalk_todo_update(union_id="uni1", task_id="TODO-1"))
    assert payload["success"] is False       # 未提供任何更新字段
    for kwargs in ({"union_id": "", "task_id": "t", "done": True},
                   {"union_id": "uni1", "task_id": "", "done": True}):
        payload = _payload(module.dingtalk_todo_update(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_todo_list_success_and_validation(api):
    module, calls, responses = api
    responses["/tasks/list"] = {
        "nextToken": "NT2", "totalCount": 1,
        "todoCards": [{"taskId": "TODO-1", "subject": "写周报", "isDone": False,
                       "todoStatus": "PENDING", "priority": 10, "dueTime": 1,
                       "creatorId": "uni1", "createdTime": 2, "modifiedTime": 3}],
    }
    payload = _payload(module.dingtalk_todo_list("uni1", is_done=False, next_token="NT1"))
    assert payload["success"] is True
    assert payload["todos"][0]["task_id"] == "TODO-1"
    assert payload["next_token"] == "NT2"
    assert calls[0]["json"] == {"isDone": False, "nextToken": "NT1"}

    payload = _payload(module.dingtalk_todo_list(""))
    assert payload["success"] is False
    assert "union_id" in payload["error"]
    assert len(calls) == 1


# ── 日程 ──

def test_calendar_create_event_success_and_validation(api):
    module, calls, responses = api
    responses["/events"] = {"id": "EV-1"}
    payload = _payload(module.dingtalk_calendar_create_event(
        user_id="uni1", summary="周会", start_time="2026-09-24T10:00:00+08:00",
        end_time="2026-09-24T11:00:00+08:00",
        description="同步进度", location="301", attendees="u1,u2"))
    assert payload == {"success": True, "event_id": "EV-1"}
    body = calls[0]["json"]
    assert body["summary"] == "周会"
    assert body["start"] == {"dateTime": "2026-09-24T10:00:00+08:00", "timeZone": "Asia/Shanghai"}
    assert body["location"] == {"displayName": "301"}
    assert body["attendees"] == [{"id": "u1"}, {"id": "u2"}]

    # 全天日程走 date 字段
    _payload(module.dingtalk_calendar_create_event(
        user_id="uni1", summary="全天", start_time="2026-09-24",
        end_time="2026-09-25", is_all_day=True))
    assert calls[1]["json"]["start"] == {"date": "2026-09-24", "timeZone": "Asia/Shanghai"}

    for kwargs in (
        {"user_id": "", "summary": "s", "start_time": "a", "end_time": "b"},
        {"user_id": "uni1", "summary": "", "start_time": "a", "end_time": "b"},
        {"user_id": "uni1", "summary": "s", "start_time": "", "end_time": "b"},
        {"user_id": "uni1", "summary": "s", "start_time": "a", "end_time": ""},
    ):
        payload = _payload(module.dingtalk_calendar_create_event(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 2


def test_calendar_list_events_success_and_validation(api):
    module, calls, responses = api
    responses["/events"] = {
        "events": [{"id": "EV-1", "summary": "周会", "status": "confirmed",
                    "start": {"dateTime": "2026-09-24T10:00:00+08:00"},
                    "end": {"dateTime": "2026-09-24T11:00:00+08:00"},
                    "location": {"displayName": "301"}}],
        "nextToken": "NT2",
    }
    payload = _payload(module.dingtalk_calendar_list_events(
        user_id="uni1", time_min="2026-09-01T00:00Z", time_max="2026-10-01T00:00Z",
        max_results=500))
    assert payload["success"] is True
    assert payload["events"][0]["event_id"] == "EV-1"
    assert payload["next_token"] == "NT2"
    assert calls[0]["method"] == "GET"
    assert calls[0]["params"]["maxResults"] == 100
    assert calls[0]["params"]["timeMin"] == "2026-09-01T00:00Z"

    payload = _payload(module.dingtalk_calendar_list_events(""))
    assert payload["success"] is False
    assert len(calls) == 1


def test_calendar_freebusy_success_and_validation(api):
    module, calls, responses = api
    responses["/querySchedule"] = {
        "scheduleInformation": [{
            "userId": "u1", "error": "",
            "scheduleItems": [{"start": {"dateTime": "2026-09-24T10:00:00+08:00"},
                               "end": {"dateTime": "2026-09-24T11:00:00+08:00"},
                               "status": "busy"}],
        }],
    }
    payload = _payload(module.dingtalk_calendar_freebusy(
        user_id="uni1", user_ids="u1,u2",
        start_time="2026-09-24T00:00Z", end_time="2026-09-25T00:00Z"))
    assert payload["success"] is True
    assert payload["schedule"][0]["user_id"] == "u1"
    assert payload["schedule"][0]["items"][0]["status"] == "busy"
    assert calls[0]["json"]["userIds"] == ["u1", "u2"]

    for kwargs in (
        {"user_id": "", "user_ids": "u1", "start_time": "a", "end_time": "b"},
        {"user_id": "uni1", "user_ids": "", "start_time": "a", "end_time": "b"},
        {"user_id": "uni1", "user_ids": "u1", "start_time": "", "end_time": "b"},
        {"user_id": "uni1", "user_ids": "u1", "start_time": "a", "end_time": ""},
    ):
        payload = _payload(module.dingtalk_calendar_freebusy(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_api_error_is_reported_without_raising(api):
    module, calls, responses = api
    responses["/querySchedule"] = RuntimeError("HTTP 500: boom")
    payload = _payload(module.dingtalk_calendar_freebusy(
        user_id="uni1", user_ids="u1", start_time="a", end_time="b"))
    assert payload["success"] is False
    assert "HTTP 500" in payload["error"]


# ── 注解 ──

async def test_new_tools_annotations():
    module = _load()
    from mcp import Client
    async with Client(module.mcp, raise_exceptions=True) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    read_tools = ("dingtalk_approval_instance", "dingtalk_approval_tasks",
                  "dingtalk_todo_list", "dingtalk_calendar_list_events",
                  "dingtalk_calendar_freebusy")
    safe_write_tools = ("dingtalk_approval_start", "dingtalk_todo_create",
                        "dingtalk_todo_update", "dingtalk_calendar_create_event")
    for name in read_tools:
        assert tools[name].annotations.read_only_hint is True, name
        assert tools[name].annotations.destructive_hint is False, name
    for name in safe_write_tools:
        assert tools[name].annotations.read_only_hint is False, name
        assert tools[name].annotations.destructive_hint is False, name
    assert tools["dingtalk_approval_action"].annotations.read_only_hint is False
    assert tools["dingtalk_approval_action"].annotations.destructive_hint is True
