"""钉钉连接器(MCP)办公 API 扩展测试: 审批/待办/日程 + P0/P1 扩展 + 卡片/文档。

P0: 审批评论/撤销/schema、消息撤回/已读、群管理(建群/成员/公告)。
P1: 公告、钉钉日志、钉盘/文件消息、视频会议、通讯录进阶、日历更新/删除。
本批: 互动/AI 卡片(模板/发卡/更新)、钉钉文档/知识库(空间/列表/信息/创建/成员)。

用 monkeypatch 替换模块内 ``_api`` 注入假 HTTP: 每个新工具覆盖成功路径与
参数必填/非法路径(参数错误不得触达 HTTP), 并断言工具注解(查询 read / 普通写
非破坏 / 撤回/删除/终止类 destructive)与工具总数(61)。
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

    def fake_api(method, path, *, params=None, json_body=None, data=None,
                 files=None, timeout=10):
        calls.append({"method": method, "path": path, "params": params, "json": json_body,
                      "data": data, "files": files})
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
        process_code="PROC-1", dingtalk_userid="user1",
        form_values='[{"name":"金额","value":"100"}]',
        dept_id=42, dingtalk_userids="user2,user3"))
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
        {"process_code": "", "dingtalk_userid": "u1"},
        {"process_code": "P1", "dingtalk_userid": ""},
        {"process_code": "P1", "dingtalk_userid": "u1", "form_values": "not-json"},
        {"process_code": "P1", "dingtalk_userid": "u1", "form_values": '{"name":"x"}'},
        {"process_code": "P1", "dingtalk_userid": "u1", "form_values": '[{"value":"x"}]'},
        {"process_code": "P1", "dingtalk_userid": "u1", "approvers": "bad-json"},
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

    for kwargs in ({"dingtalk_userid": ""}, {"dingtalk_userid": "u1", "status": 2}, {"dingtalk_userid": "u1", "status": True}):
        payload = _payload(module.dingtalk_approval_tasks(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_approval_action_success_and_aliases(api):
    module, calls, responses = api
    responses["/v1.0/workflow/processInstances/execute"] = {"success": True, "result": True}
    payload = _payload(module.dingtalk_approval_action(
        task_id=9, result="同意", remark="同意报销", process_instance_id="PI-1", dingtalk_userid="user1"))
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
        dingtalk_unionid="uni1", subject="写周报", dingtalk_userids="user1,user2",
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
        {"dingtalk_unionid": "", "subject": "s", "dingtalk_userids": "u1"},
        {"dingtalk_unionid": "uni1", "subject": "", "dingtalk_userids": "u1"},
        {"dingtalk_unionid": "uni1", "subject": "s", "dingtalk_userids": ""},
    ):
        payload = _payload(module.dingtalk_todo_create(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_todo_update_success_and_validation(api):
    module, calls, responses = api
    responses["/tasks/TODO-1"] = {"result": True}
    payload = _payload(module.dingtalk_todo_update(
        dingtalk_unionid="uni1", task_id="TODO-1", done=True, description="已完成"))
    assert payload["success"] is True
    assert calls[0]["method"] == "PUT"
    assert calls[0]["json"] == {"done": True, "description": "已完成"}

    payload = _payload(module.dingtalk_todo_update(dingtalk_unionid="uni1", task_id="TODO-1"))
    assert payload["success"] is False       # 未提供任何更新字段
    for kwargs in ({"dingtalk_unionid": "", "task_id": "t", "done": True},
                   {"dingtalk_unionid": "uni1", "task_id": "", "done": True}):
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
    assert "dingtalk_unionid" in payload["error"]
    assert len(calls) == 1


# ── 日程 ──

def test_calendar_create_event_success_and_validation(api):
    module, calls, responses = api
    responses["/events"] = {"id": "EV-1"}
    payload = _payload(module.dingtalk_calendar_create_event(
        dingtalk_unionid="uni1", summary="周会", start_time="2026-09-24T10:00:00+08:00",
        end_time="2026-09-24T11:00:00+08:00",
        description="同步进度", location="301", dingtalk_userids="u1,u2"))
    assert payload == {"success": True, "event_id": "EV-1"}
    body = calls[0]["json"]
    assert body["summary"] == "周会"
    assert body["start"] == {"dateTime": "2026-09-24T10:00:00+08:00", "timeZone": "Asia/Shanghai"}
    assert body["location"] == {"displayName": "301"}
    assert body["attendees"] == [{"id": "u1"}, {"id": "u2"}]

    # 全天日程走 date 字段
    _payload(module.dingtalk_calendar_create_event(
        dingtalk_unionid="uni1", summary="全天", start_time="2026-09-24",
        end_time="2026-09-25", is_all_day=True))
    assert calls[1]["json"]["start"] == {"date": "2026-09-24", "timeZone": "Asia/Shanghai"}

    for kwargs in (
        {"dingtalk_unionid": "", "summary": "s", "start_time": "a", "end_time": "b"},
        {"dingtalk_unionid": "uni1", "summary": "", "start_time": "a", "end_time": "b"},
        {"dingtalk_unionid": "uni1", "summary": "s", "start_time": "", "end_time": "b"},
        {"dingtalk_unionid": "uni1", "summary": "s", "start_time": "a", "end_time": ""},
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
        dingtalk_unionid="uni1", time_min="2026-09-01T00:00Z", time_max="2026-10-01T00:00Z",
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
        dingtalk_unionid="uni1", dingtalk_unionids="u1,u2",
        start_time="2026-09-24T00:00Z", end_time="2026-09-25T00:00Z"))
    assert payload["success"] is True
    assert payload["schedule"][0]["user_id"] == "u1"
    assert payload["schedule"][0]["items"][0]["status"] == "busy"
    assert calls[0]["json"]["userIds"] == ["u1", "u2"]

    for kwargs in (
        {"dingtalk_unionid": "", "dingtalk_unionids": "u1", "start_time": "a", "end_time": "b"},
        {"dingtalk_unionid": "uni1", "dingtalk_unionids": "", "start_time": "a", "end_time": "b"},
        {"dingtalk_unionid": "uni1", "dingtalk_unionids": "u1", "start_time": "", "end_time": "b"},
        {"dingtalk_unionid": "uni1", "dingtalk_unionids": "u1", "start_time": "a", "end_time": ""},
    ):
        payload = _payload(module.dingtalk_calendar_freebusy(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_api_error_is_reported_without_raising(api):
    module, calls, responses = api
    responses["/querySchedule"] = RuntimeError("HTTP 500: boom")
    payload = _payload(module.dingtalk_calendar_freebusy(
        dingtalk_unionid="uni1", dingtalk_unionids="u1", start_time="a", end_time="b"))
    assert payload["success"] is False
    assert "HTTP 500" in payload["error"]


# ── 审批补全 ──

def test_approval_comment_success_and_validation(api):
    module, calls, responses = api
    responses["/processInstances/comments"] = {"commentId": "C1"}
    payload = _payload(module.dingtalk_approval_comment(
        process_instance_id="PI-1", text="同意报销", dingtalk_userid="user1",
        file='{"fileId":"F1","fileName":"a.pdf"}'))
    assert payload["success"] is True
    assert payload["comment_id"] == "C1"
    body = calls[0]["json"]
    assert body["processInstanceId"] == "PI-1"
    assert body["text"] == "同意报销"
    assert body["commentUserId"] == "user1"
    assert body["file"] == {"fileId": "F1", "fileName": "a.pdf"}
    assert calls[0]["method"] == "POST"

    for kwargs in (
        {"process_instance_id": "", "text": "x"},
        {"process_instance_id": "PI-1", "text": ""},
        {"process_instance_id": "PI-1", "text": "x", "file": "not-json"},
        {"process_instance_id": "PI-1", "text": "x", "file": ["x"]},
    ):
        payload = _payload(module.dingtalk_approval_comment(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_approval_revoke_success_and_validation(api):
    module, calls, responses = api
    responses["/processInstances/terminate"] = {"success": True, "result": True}
    payload = _payload(module.dingtalk_approval_revoke("PI-1", comment="重复提交"))
    assert payload["success"] is True
    assert calls[0]["json"] == {"processInstanceId": "PI-1", "comment": "重复提交"}

    responses["/processInstances/terminate"] = {"success": False, "result": False}
    payload = _payload(module.dingtalk_approval_revoke("PI-1"))
    assert payload["success"] is False
    assert calls[1]["json"] == {"processInstanceId": "PI-1"}

    payload = _payload(module.dingtalk_approval_revoke(""))
    assert payload["success"] is False
    assert len(calls) == 2


def test_approval_schema_success_and_validation(api):
    module, calls, responses = api
    responses["/forms/schemas/processCodes"] = {
        "result": {"processCode": "PROC-1", "formComponentList": [{"id": "c1"}]}}
    payload = _payload(module.dingtalk_approval_schema("PROC-1"))
    assert payload["success"] is True
    assert payload["schema"]["formComponentList"] == [{"id": "c1"}]
    assert calls[0]["method"] == "GET"
    assert calls[0]["params"] == {"processCode": "PROC-1"}

    payload = _payload(module.dingtalk_approval_schema(""))
    assert payload["success"] is False
    assert len(calls) == 1


# ── 消息治理 ──

def test_recall_single_success_and_validation(api):
    module, calls, responses = api
    responses["/oToMessages/batchRecall"] = {"processQueryKeys": ["k1"]}
    payload = _payload(module.dingtalk_recall_single(process_query_key="PQ-1"))
    assert payload["success"] is True
    assert calls[0]["json"] == {"robotCode": module.ROBOT_CODE, "processQueryKey": "PQ-1"}

    payload = _payload(module.dingtalk_recall_single(msg_id="MID-1"))
    assert payload["success"] is True
    assert calls[1]["json"] == {"robotCode": module.ROBOT_CODE, "msgId": "MID-1"}

    payload = _payload(module.dingtalk_recall_single())
    assert payload["success"] is False
    assert "至少" in payload["error"]
    assert len(calls) == 2


def test_recall_group_success_and_validation(api):
    module, calls, responses = api
    responses["/groupMessages/batchRecall"] = {"processQueryKeys": ["k2"]}
    payload = _payload(module.dingtalk_recall_group(msg_id="MID-2"))
    assert payload["success"] is True
    assert calls[0]["json"] == {"robotCode": module.ROBOT_CODE, "msgId": "MID-2"}

    payload = _payload(module.dingtalk_recall_group())
    assert payload["success"] is False
    assert len(calls) == 1


def test_message_read_status_success_and_validation(api):
    module, calls, responses = api
    responses["/oToMessages/readStatus"] = {"readStatusList": [{"userId": "u1"}]}
    payload = _payload(module.dingtalk_message_read_status("PQ-1", scene="single"))
    assert payload["success"] is True
    assert payload["scene"] == "single"
    assert payload["read_status"] == {"readStatusList": [{"userId": "u1"}]}
    assert calls[0]["path"] == "/v1.0/robot/oToMessages/readStatus"
    assert calls[0]["params"] == {"robotCode": module.ROBOT_CODE, "processQueryKey": "PQ-1"}

    responses["/groupMessages/readStatus"] = {"readStatus": "ALL_READ"}
    payload = _payload(module.dingtalk_message_read_status("PQ-2", scene="group"))
    assert payload["success"] is True
    assert calls[1]["path"] == "/v1.0/robot/groupMessages/readStatus"

    for kwargs in ({"process_query_key": ""}, {"process_query_key": "k", "scene": "all"}):
        payload = _payload(module.dingtalk_message_read_status(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 2


# ── 群管理 ──

def test_create_group_success_and_validation(api):
    module, calls, responses = api
    responses["/v1.0/im/chatGroups"] = {"chatId": "CHAT-1", "openConversationId": "cidX"}
    payload = _payload(module.dingtalk_create_group(
        name="项目群", dingtalk_userid="u1", dingtalk_userids="u2,u3",
        icon="MEDIA-1", only_admin_can_invite=True))
    assert payload["success"] is True
    assert payload["chat_id"] == "CHAT-1"
    assert payload["open_conversation_id"] == "cidX"
    assert calls[0]["json"] == {"name": "项目群", "owner": "u1",
                                "memberUserIds": ["u2", "u3"],
                                "icon": "MEDIA-1", "onlyAdminCanInvite": True}

    for kwargs in ({"name": ""}, {"name": "x" * 101}):
        payload = _payload(module.dingtalk_create_group(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_group_add_members_success_and_validation(api):
    module, calls, responses = api
    responses["/members"] = {"result": True}
    payload = _payload(module.dingtalk_group_add_members("CHAT-1", "u1,u2"))
    assert payload["success"] is True
    assert payload["added"] == 2
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/v1.0/im/chatGroups/CHAT-1/members"
    assert calls[0]["json"] == {"userIds": ["u1", "u2"]}

    for kwargs in ({"chat_id": "", "dingtalk_userids": "u1"}, {"chat_id": "c", "dingtalk_userids": ""}):
        payload = _payload(module.dingtalk_group_add_members(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_group_remove_members_success_and_validation(api):
    module, calls, responses = api
    responses["/members"] = {"result": True}
    payload = _payload(module.dingtalk_group_remove_members("CHAT-1", "u1"))
    assert payload["success"] is True
    assert payload["removed"] == 1
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["json"] == {"userIds": ["u1"]}

    payload = _payload(module.dingtalk_group_remove_members("", "u1"))
    assert payload["success"] is False
    assert len(calls) == 1


def test_group_set_notice_success_and_validation(api):
    module, calls, responses = api
    responses["/v1.0/im/chatGroups/"] = {"result": True}
    payload = _payload(module.dingtalk_group_set_notice("CHAT-1", "本周五团建"))
    assert payload["success"] is True
    assert calls[0]["method"] == "PUT"
    assert calls[0]["path"] == "/v1.0/im/chatGroups/CHAT-1"
    assert calls[0]["json"] == {"notice": "本周五团建"}

    for kwargs in ({"chat_id": "", "notice": "x"}, {"chat_id": "c", "notice": " "}):
        payload = _payload(module.dingtalk_group_set_notice(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


# ── 公告 ──

def test_announcement_create_success_and_validation(api):
    module, calls, responses = api
    responses["/blackboard/blackboards"] = {"boardId": "B1"}
    payload = _payload(module.dingtalk_announcement_create(
        "放假通知", "国庆放假 7 天", dingtalk_userid="u1", dingtalk_userids="u1,u2"))
    assert payload["success"] is True
    assert payload["board_id"] == "B1"
    assert calls[0]["json"] == {"title": "放假通知", "content": "国庆放假 7 天",
                                "author": "u1", "sendTo": ["u1", "u2"]}

    for kwargs in ({"title": "", "content": "x"}, {"title": "t", "content": ""}):
        payload = _payload(module.dingtalk_announcement_create(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_announcement_delete_success_and_validation(api):
    module, calls, responses = api
    responses["/blackboard/blackboards/B1"] = {"result": True}
    payload = _payload(module.dingtalk_announcement_delete("B1"))
    assert payload["success"] is True
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["path"] == "/v1.0/blackboard/blackboards/B1"

    payload = _payload(module.dingtalk_announcement_delete(""))
    assert payload["success"] is False
    assert len(calls) == 1


# ── 钉钉日志 ──

def test_report_submit_success_and_validation(api):
    module, calls, responses = api
    responses["/v1.0/report/entries"] = {"id": "E1"}
    payload = _payload(module.dingtalk_report_submit(
        "日报", "完成 A/B", dingtalk_userids="u1", to_chat=True))
    assert payload["success"] is True
    assert payload["entry_id"] == "E1"
    assert calls[0]["json"] == {"templateName": "日报", "content": "完成 A/B",
                                "userIds": ["u1"], "toChat": True}

    for kwargs in ({"template_name": "", "content": "x"},
                   {"template_name": "t", "content": ""}):
        payload = _payload(module.dingtalk_report_submit(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_report_list_success_and_validation(api):
    module, calls, responses = api
    responses["/v1.0/report/entries"] = {
        "entries": [{"entryId": "E1", "templateName": "日报"}],
        "hasMore": True, "nextCursor": 3}
    payload = _payload(module.dingtalk_report_list(
        dingtalk_userids="u1,u2", start_time=1750000000000, end_time=1751000000000,
        template_name="日报", size=500))
    assert payload["success"] is True
    assert payload["entries"][0]["entryId"] == "E1"
    assert payload["has_more"] is True
    assert payload["next_cursor"] == 3
    params = calls[0]["params"]
    assert params["userIds"] == ["u1", "u2"]
    assert params["size"] == 100
    assert params["startTime"] == 1750000000000
    assert params["templateName"] == "日报"

    for kwargs in ({"size": 0}, {"cursor": -1}, {"start_time": "abc"}, {"end_time": True}):
        payload = _payload(module.dingtalk_report_list(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_report_templates_success(api):
    module, calls, responses = api
    responses["/v1.0/report/templates"] = {"templates": [{"name": "日报"}]}
    payload = _payload(module.dingtalk_report_templates())
    assert payload["success"] is True
    assert payload["templates"] == [{"name": "日报"}]
    assert payload["count"] == 1
    assert calls[0]["method"] == "GET"


# ── 钉盘/文件 ──

def test_drive_upload_success_and_validation(api, tmp_path):
    module, calls, responses = api
    responses["/files/upload"] = {"dentry": {"id": "D1", "name": "改名.txt"}}
    local = tmp_path / "a.txt"
    local.write_text("hello", encoding="utf-8")
    payload = _payload(module.dingtalk_drive_upload(
        space_id="S1", parent_dentry_id="P0", file_path=str(local),
        name="改名.txt", conflict_policy="overwrite"))
    assert payload["success"] is True
    assert payload["dentry_id"] == "D1"
    assert payload["name"] == "改名.txt"
    call = calls[0]
    assert call["path"] == "/v1.0/storage/spaces/S1/files/upload"
    assert call["data"] == {"parentDentryId": "P0", "conflictPolicy": "OVERWRITE"}
    assert call["files"]["file"][0] == "改名.txt"

    for kwargs in (
        {"space_id": "", "parent_dentry_id": "P0", "file_path": str(local)},
        {"space_id": "S1", "parent_dentry_id": "", "file_path": str(local)},
        {"space_id": "S1", "parent_dentry_id": "P0", "file_path": ""},
        {"space_id": "S1", "parent_dentry_id": "P0",
         "file_path": str(tmp_path / "nope.txt")},
    ):
        payload = _payload(module.dingtalk_drive_upload(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_drive_download_url_success_and_validation(api):
    module, calls, responses = api
    responses["/downloadInfos"] = {"resourceUrl": "https://x/dl"}
    payload = _payload(module.dingtalk_drive_download_url("S1", "D1"))
    assert payload["success"] is True
    assert payload["download_url"] == "https://x/dl"
    assert calls[0]["path"] == "/v1.0/storage/spaces/S1/dentries/D1/downloadInfos"

    for kwargs in ({"space_id": "", "dentry_id": "D1"}, {"space_id": "S1", "dentry_id": ""}):
        payload = _payload(module.dingtalk_drive_download_url(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_send_file_single_success_and_validation(api, tmp_path):
    module, calls, responses = api
    responses["/robot/messages/upload"] = {"mediaId": "M1"}
    responses["/robot/oToMessages/batchSend"] = {"processQueryKey": "PQ1"}
    local = tmp_path / "report.pdf"
    local.write_bytes(b"%PDF-1.4 test")
    payload = _payload(module.dingtalk_send_file_single("user1,user2", str(local)))
    assert payload["success"] is True
    assert payload["media_id"] == "M1"
    assert len(calls) == 2
    upload = calls[0]
    assert upload["path"] == "/v1.0/robot/messages/upload"
    assert upload["params"] == {"type": "file"}
    assert upload["files"]["file"][0] == "report.pdf"
    send = calls[1]
    assert send["path"] == "/v1.0/robot/oToMessages/batchSend"
    assert send["json"]["userIds"] == ["user1", "user2"]
    assert send["json"]["msgKey"] == "sampleFile"
    assert json.loads(send["json"]["msgParam"]) == {
        "mediaId": "M1", "fileName": "report.pdf", "fileType": "pdf"}

    for kwargs in (
        {"dingtalk_userids": "", "file_path": str(local)},
        {"dingtalk_userids": "u1", "file_path": ""},
        {"dingtalk_userids": "u1", "file_path": str(tmp_path / "nope.bin")},
    ):
        payload = _payload(module.dingtalk_send_file_single(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 2


def test_send_file_group_success_and_validation(api, tmp_path):
    module, calls, responses = api
    responses["/robot/messages/upload"] = {"mediaId": "M2"}
    responses["/robot/groupMessages/send"] = {"processQueryKey": "PQ2"}
    local = tmp_path / "doc.docx"
    local.write_bytes(b"doc")
    payload = _payload(module.dingtalk_send_file_group("cidX", str(local)))
    assert payload["success"] is True
    assert payload["process_query_key"] == "PQ2"
    send = calls[1]
    assert send["path"] == "/v1.0/robot/groupMessages/send"
    assert send["json"]["conversationId"] == "cidX"
    assert send["json"]["msgKey"] == "sampleFile"

    for kwargs in (
        {"open_conversation_id": "", "file_path": str(local)},
        {"open_conversation_id": "cidX", "file_path": str(tmp_path / "nope.bin")},
    ):
        payload = _payload(module.dingtalk_send_file_group(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 2


def test_upload_media_failure_is_reported(api, tmp_path):
    module, calls, responses = api
    responses["/robot/messages/upload"] = {"message": "upload fail"}
    local = tmp_path / "a.txt"
    local.write_text("x", encoding="utf-8")
    payload = _payload(module.dingtalk_send_file_single("u1", str(local)))
    assert payload["success"] is False
    assert "mediaId" in payload["error"]


def test_api_sends_multipart_without_json_content_type(monkeypatch, tmp_path):
    module = _load()
    captured = {}

    class FakeResponse:
        status_code = 200
        text = '{"ok":true}'

        def json(self):
            return {"ok": True}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(module.requests, "request", fake_request)
    monkeypatch.setattr(module, "_get_access_token", lambda: "tok")
    local = tmp_path / "a.txt"
    local.write_text("x", encoding="utf-8")
    with open(local, "rb") as fh:
        result = module._api("POST", "/v1.0/robot/messages/upload",
                             params={"type": "file"},
                             files={"file": ("a.txt", fh)})
    assert result == {"ok": True}
    assert "Content-Type" not in captured["headers"]
    assert captured["params"] == {"type": "file"}
    assert captured["files"]["file"][0] == "a.txt"

    module._api("GET", "/v1.0/report/templates")
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["url"].endswith("/v1.0/report/templates")


# ── 视频会议 ──

def test_conference_create_success_and_validation(api):
    module, calls, responses = api
    responses["/conference/videoConferences"] = {"conferenceId": "CONF-1"}
    payload = _payload(module.dingtalk_conference_create(
        "周会", 1750000000000, 1750003600000, dingtalk_userids="u1,u2"))
    assert payload["success"] is True
    assert payload["conference_id"] == "CONF-1"
    assert calls[0]["json"] == {"title": "周会", "startTime": 1750000000000,
                                "endTime": 1750003600000, "memberUserIds": ["u1", "u2"]}

    for kwargs in (
        {"title": "", "start_time": 1, "end_time": 2},
        {"title": "t", "start_time": 0, "end_time": 2},
        {"title": "t", "start_time": 5, "end_time": 5},
        {"title": "t", "start_time": True, "end_time": 2},
    ):
        payload = _payload(module.dingtalk_conference_create(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_conference_query_success_and_validation(api):
    module, calls, responses = api
    responses["/conference/videoConferences/CONF-1"] = {"title": "周会", "status": "OPEN"}
    payload = _payload(module.dingtalk_conference_query("CONF-1"))
    assert payload["success"] is True
    assert payload["conference"]["status"] == "OPEN"
    assert calls[0]["method"] == "GET"

    payload = _payload(module.dingtalk_conference_query(""))
    assert payload["success"] is False
    assert len(calls) == 1


def test_conference_close_success_and_validation(api):
    module, calls, responses = api
    responses["/conference/videoConferences/CONF-1/close"] = {"result": True}
    payload = _payload(module.dingtalk_conference_close("CONF-1"))
    assert payload["success"] is True
    assert calls[0]["method"] == "PUT"
    assert calls[0]["path"].endswith("/close")

    payload = _payload(module.dingtalk_conference_close(""))
    assert payload["success"] is False
    assert len(calls) == 1


# ── 通讯录进阶 ──

def test_user_by_unionid_success_and_validation(api):
    module, calls, responses = api
    responses["/v1.0/contact/users/"] = {"unionId": "uni1", "nick": "张三"}
    payload = _payload(module.dingtalk_user_by_unionid("uni1"))
    assert payload["success"] is True
    assert payload["user"]["nick"] == "张三"
    assert calls[0]["path"] == "/v1.0/contact/users/uni1"

    payload = _payload(module.dingtalk_user_by_unionid(""))
    assert payload["success"] is False
    assert len(calls) == 1


def test_department_detail_success_and_validation(api):
    module, calls, responses = api
    responses["/v1.0/contact/departments/"] = {"deptId": 42, "name": "研发部"}
    payload = _payload(module.dingtalk_department_detail(42))
    assert payload["success"] is True
    assert payload["department"]["name"] == "研发部"
    assert calls[0]["path"] == "/v1.0/contact/departments/42"

    for value in ("", 0, None):
        payload = _payload(module.dingtalk_department_detail(value))
        assert payload["success"] is False
    assert len(calls) == 1


def test_role_list_success(api):
    module, calls, responses = api
    responses["/v1.0/contact/roles"] = {"roles": [{"id": 1, "name": "主管理员"}]}
    payload = _payload(module.dingtalk_role_list())
    assert payload["success"] is True
    assert payload["roles"][0]["name"] == "主管理员"
    assert calls[0]["method"] == "GET"
    assert calls[0]["path"] == "/v1.0/contact/roles"


def test_external_contacts_success_and_validation(api):
    module, calls, responses = api
    responses["/empExtContacts"] = {"contacts": [{"userId": "u1"}], "nextCursor": 9}
    payload = _payload(module.dingtalk_external_contacts(
        dingtalk_userids="u1,u2", size=500, cursor=3))
    assert payload["success"] is True
    assert payload["contacts"] == [{"userId": "u1"}]
    assert payload["next_cursor"] == 9
    params = calls[0]["params"]
    assert params["userIds"] == ["u1", "u2"]
    assert params["size"] == 100
    assert params["cursor"] == 3

    for kwargs in ({"size": 0}, {"cursor": -1}):
        payload = _payload(module.dingtalk_external_contacts(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


# ── 日历补全 ──

def test_calendar_update_event_success_and_validation(api):
    module, calls, responses = api
    responses["/events/EV-1"] = {"result": True}
    payload = _payload(module.dingtalk_calendar_update_event(
        dingtalk_unionid="uni1", calendar_id="primary", event_id="EV-1",
        summary="新标题", start="2026-09-24T11:00:00+08:00",
        end="2026-09-24T12:00:00+08:00", description="改期"))
    assert payload["success"] is True
    assert calls[0]["method"] == "PUT"
    assert calls[0]["path"] == "/v1.0/calendar/users/uni1/calendars/primary/events/EV-1"
    body = calls[0]["json"]
    assert body["summary"] == "新标题"
    assert body["start"] == {"dateTime": "2026-09-24T11:00:00+08:00",
                             "timeZone": "Asia/Shanghai"}
    assert body["end"] == {"dateTime": "2026-09-24T12:00:00+08:00",
                           "timeZone": "Asia/Shanghai"}
    assert body["description"] == "改期"

    payload = _payload(module.dingtalk_calendar_update_event("uni1", "primary", "EV-1"))
    assert payload["success"] is False       # 未提供任何更新字段
    for kwargs in (
        {"dingtalk_unionid": "", "calendar_id": "p", "event_id": "e"},
        {"dingtalk_unionid": "u", "calendar_id": "", "event_id": "e"},
        {"dingtalk_unionid": "u", "calendar_id": "p", "event_id": ""},
    ):
        payload = _payload(module.dingtalk_calendar_update_event(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_calendar_delete_event_success_and_validation(api):
    module, calls, responses = api
    responses["/events/EV-1"] = {"result": True}
    payload = _payload(module.dingtalk_calendar_delete_event("uni1", "primary", "EV-1"))
    assert payload["success"] is True
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["path"] == "/v1.0/calendar/users/uni1/calendars/primary/events/EV-1"

    payload = _payload(module.dingtalk_calendar_delete_event("uni1", "primary", ""))
    assert payload["success"] is False
    assert len(calls) == 1


def test_new_tools_api_error_passthrough(api):
    module, calls, responses = api
    responses["/v1.0/report/templates"] = RuntimeError("HTTP 403: Forbidden")
    payload = _payload(module.dingtalk_report_templates())
    assert payload["success"] is False
    assert "HTTP 403" in payload["error"]


# ── 互动/AI 卡片 ──

def test_card_template_list_success(api):
    module, calls, responses = api
    responses["/v1.0/card/templates"] = {"templates": [{"id": "T1", "name": "AI 卡片"}]}
    payload = _payload(module.dingtalk_card_template_list())
    assert payload["success"] is True
    assert payload["templates"][0]["id"] == "T1"
    assert payload["count"] == 1
    assert calls[0]["method"] == "GET"
    assert calls[0]["path"] == "/v1.0/card/templates"

    # 字段名兜底: templateList / data
    responses["/v1.0/card/templates"] = {"templateList": [{"id": "T2"}]}
    payload = _payload(module.dingtalk_card_template_list())
    assert payload["templates"] == [{"id": "T2"}]
    responses["/v1.0/card/templates"] = {"data": {"items": []}}
    payload = _payload(module.dingtalk_card_template_list())
    assert payload["templates"] == [] and payload["count"] == 0


def test_ai_card_send_success_and_validation(api):
    module, calls, responses = api
    # 键按插入顺序匹配: deliver 键须先于 instances(前者是后者的超集)注册
    responses["/v1.0/card/instances/deliver"] = {"success": True}
    responses["/v1.0/card/instances"] = {"success": True}
    payload = _payload(module.dingtalk_ai_card_send(
        template_id="TPL-1", card_data='{"msgTitle":"标题"}',
        dingtalk_userid="staff1", out_track_id="track-1"))
    assert payload["success"] is True
    assert payload["out_track_id"] == "track-1"
    create = calls[0]
    assert create["method"] == "POST"
    assert create["path"] == "/v1.0/card/instances"
    assert create["params"] == {"callbackType": "STREAM"}
    assert create["json"]["cardTemplateId"] == "TPL-1"
    assert create["json"]["outTrackId"] == "track-1"
    assert create["json"]["callbackType"] == "STREAM"
    assert create["json"]["cardData"] == {"cardParamMap": {"msgTitle": "标题"}}
    deliver = calls[1]
    assert deliver["path"] == "/v1.0/card/instances/deliver"
    assert deliver["json"] == {"outTrackId": "track-1",
                               "openSpaceId": "dtv1.card//IM_ROBOT.staff1",
                               "userIdType": 1,
                               "imRobotOpenDeliverModel": {"spaceType": "IM_ROBOT"}}

    # 群 + 私聊双场域逐一投递; cardParamMap 形态原样透传; outTrackId 自动生成
    payload = _payload(module.dingtalk_ai_card_send(
        template_id="TPL-1", card_data='{"cardParamMap":{"content":"x"}}',
        dingtalk_userid="u2", open_conversation_id="cidX"))
    assert payload["success"] is True
    assert payload["out_track_id"].startswith("mcpcard_")
    assert calls[2]["json"]["cardData"] == {"cardParamMap": {"content": "x"}}
    assert calls[3]["json"]["openSpaceId"] == "dtv1.card//IM_ROBOT.u2"
    assert calls[4]["json"]["openSpaceId"] == "dtv1.card//IM_GROUP.cidX"
    assert calls[4]["json"]["imGroupOpenDeliverModel"] == {"robotCode": module.ROBOT_CODE}

    for kwargs in (
        {"template_id": "", "card_data": "{}", "dingtalk_userid": "u1"},
        {"template_id": "T", "card_data": "not-json", "dingtalk_userid": "u1"},
        {"template_id": "T", "card_data": "", "dingtalk_userid": "u1"},
        {"template_id": "T", "card_data": "{}", "dingtalk_userid": "u1"},
        {"template_id": "T", "card_data": "{}"},
    ):
        payload = _payload(module.dingtalk_ai_card_send(**kwargs))
        assert payload["success"] is False
        assert payload["error"]
    assert len(calls) == 5


def test_ai_card_send_deliver_error_keeps_track_id(api):
    module, calls, responses = api
    # 键按插入顺序匹配: deliver 键须先于 instances(前者是后者的超集)注册
    responses["/v1.0/card/instances/deliver"] = RuntimeError(
        "HTTP 400: code=InvalidParameter message=openSpaceId 非法")
    responses["/v1.0/card/instances"] = {"success": True}
    payload = _payload(module.dingtalk_ai_card_send(
        template_id="T", card_data='{"a":"1"}', dingtalk_userid="u1", out_track_id="tk"))
    assert payload["success"] is False
    assert payload["out_track_id"] == "tk"
    assert "code=InvalidParameter" in payload["error"]


def test_card_instance_update_success_and_validation(api):
    module, calls, responses = api
    responses["/v1.0/card/instances"] = {"success": True}
    payload = _payload(module.dingtalk_card_instance_update(
        out_track_id="track-1", card_data='{"cardParamMap":{"status":"done"}}'))
    assert payload["success"] is True
    call = calls[0]
    assert call["method"] == "PUT"
    assert call["path"] == "/v1.0/card/instances"
    assert call["json"] == {"outTrackId": "track-1",
                            "cardData": {"cardParamMap": {"status": "done"}},
                            "cardUpdateOptions": {"updateCardDataByKey": True}}

    responses["/v1.0/card/instances"] = {"success": False}
    payload = _payload(module.dingtalk_card_instance_update("track-1", '{"a":"1"}'))
    assert payload["success"] is False
    assert payload["error"]

    for kwargs in (
        {"out_track_id": "", "card_data": "{}"},
        {"out_track_id": "t", "card_data": "[]"},
        {"out_track_id": "t", "card_data": "bad-json"},
    ):
        payload = _payload(module.dingtalk_card_instance_update(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 2


# ── 钉钉文档/知识库 ──

def test_doc_workspaces_success(api):
    module, calls, responses = api
    responses["/v1.0/doc/workspaces"] = {"workspaces": [{"id": "WS1", "name": "研发知识库"}]}
    payload = _payload(module.dingtalk_doc_workspaces())
    assert payload["success"] is True
    assert payload["workspaces"][0]["id"] == "WS1"
    assert payload["count"] == 1
    assert calls[0]["method"] == "GET"
    assert calls[0]["path"] == "/v1.0/doc/workspaces"

    # 字段名兜底: workspaceList / data
    responses["/v1.0/doc/workspaces"] = {"workspaceList": [{"id": "WS2"}]}
    payload = _payload(module.dingtalk_doc_workspaces())
    assert payload["workspaces"] == [{"id": "WS2"}]


def test_doc_list_success_and_validation(api):
    module, calls, responses = api
    responses["/v1.0/doc/workspaces/WS1/docs"] = {
        "docs": [{"docId": "D1", "name": "设计稿"}], "nextToken": "NT2"}
    payload = _payload(module.dingtalk_doc_list(
        workspace_id="WS1", parent_id="P1", page_size=500, next_token="NT1"))
    assert payload["success"] is True
    assert payload["docs"][0]["docId"] == "D1"
    assert payload["count"] == 1
    assert payload["next_token"] == "NT2"
    assert calls[0]["path"] == "/v1.0/doc/workspaces/WS1/docs"
    assert calls[0]["params"] == {"maxResults": 100, "parentId": "P1", "nextToken": "NT1"}

    for kwargs in ({"workspace_id": ""}, {"workspace_id": "WS1", "page_size": 0},
                   {"workspace_id": "WS1", "page_size": True}):
        payload = _payload(module.dingtalk_doc_list(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_doc_info_success_and_validation(api):
    module, calls, responses = api
    responses["/v1.0/doc/workspaces/WS1/docs/D1"] = {
        "docId": "D1", "name": "设计稿", "docType": "DOC"}
    payload = _payload(module.dingtalk_doc_info("WS1", "D1"))
    assert payload["success"] is True
    assert payload["doc"]["name"] == "设计稿"
    assert calls[0]["path"] == "/v1.0/doc/workspaces/WS1/docs/D1"

    for kwargs in ({"workspace_id": "", "doc_id": "D1"},
                   {"workspace_id": "WS1", "doc_id": ""}):
        payload = _payload(module.dingtalk_doc_info(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_doc_create_success_and_validation(api):
    module, calls, responses = api
    responses["/v1.0/doc/workspaces/WS1/docs"] = {"docId": "D2", "url": "https://d/D2"}
    payload = _payload(module.dingtalk_doc_create(
        workspace_id="WS1", name="需求文档", doc_type="doc", parent_id="P1"))
    assert payload["success"] is True
    assert payload["doc_id"] == "D2"
    assert payload["url"] == "https://d/D2"
    assert calls[0]["method"] == "POST"
    assert calls[0]["json"] == {"name": "需求文档", "docType": "DOC", "parentId": "P1"}

    for kwargs in (
        {"workspace_id": "", "name": "n"},
        {"workspace_id": "WS1", "name": ""},
        {"workspace_id": "WS1", "name": "n", "doc_type": " "},
    ):
        payload = _payload(module.dingtalk_doc_create(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_doc_add_member_success_and_validation(api):
    module, calls, responses = api
    responses["/docs/D1/members"] = {"success": True}
    payload = _payload(module.dingtalk_doc_add_member(
        workspace_id="WS1", doc_id="D1", dingtalk_userid="u1", role="editor"))
    assert payload["success"] is True
    assert payload["role"] == "EDITOR"
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/v1.0/doc/workspaces/WS1/docs/D1/members"
    assert calls[0]["json"] == {"members": [
        {"memberId": "u1", "memberType": "USER", "role": "EDITOR"}]}

    for kwargs in (
        {"workspace_id": "", "doc_id": "D1", "dingtalk_userid": "u1"},
        {"workspace_id": "WS1", "doc_id": "", "dingtalk_userid": "u1"},
        {"workspace_id": "WS1", "doc_id": "D1", "dingtalk_userid": ""},
        {"workspace_id": "WS1", "doc_id": "D1", "dingtalk_userid": "u1", "role": " "},
    ):
        payload = _payload(module.dingtalk_doc_add_member(**kwargs))
        assert payload["success"] is False
    assert len(calls) == 1


def test_doc_api_error_passthrough(api):
    module, calls, responses = api
    responses["/v1.0/doc/workspaces"] = RuntimeError(
        "HTTP 403: code=Forbidden.AccessDenied message=无权限")
    payload = _payload(module.dingtalk_doc_workspaces())
    assert payload["success"] is False
    assert "code=Forbidden.AccessDenied" in payload["error"]


def test_api_error_detail_extracts_errcode_errmsg(monkeypatch):
    module = _load()

    class FakeResponse:
        status_code = 400
        text = '{"errcode":40035,"errmsg":"参数错误"}'

        def json(self):
            return {"errcode": 40035, "errmsg": "参数错误"}

    def fake_request(method, url, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(module.requests, "request", fake_request)
    monkeypatch.setattr(module, "_get_access_token", lambda: "tok")
    with pytest.raises(RuntimeError) as excinfo:
        module._api("GET", "/v1.0/doc/workspaces")
    assert "HTTP 400" in str(excinfo.value)
    assert "errcode=40035" in str(excinfo.value)
    assert "errmsg=参数错误" in str(excinfo.value)


# ── 注解与工具总数 ──

async def test_tool_annotations_and_count():
    module = _load()
    from mcp import Client
    async with Client(module.mcp, raise_exceptions=True) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert len(tools) == 61

    read_tools = (
        "dingtalk_approval_instance", "dingtalk_approval_tasks",
        "dingtalk_approval_schema",
        "dingtalk_todo_list", "dingtalk_calendar_list_events",
        "dingtalk_calendar_freebusy",
        "dingtalk_message_read_status",
        "dingtalk_report_list", "dingtalk_report_templates",
        "dingtalk_drive_download_url", "dingtalk_conference_query",
        "dingtalk_user_by_unionid", "dingtalk_department_detail",
        "dingtalk_role_list", "dingtalk_external_contacts",
        "dingtalk_card_template_list",
        "dingtalk_doc_workspaces", "dingtalk_doc_list", "dingtalk_doc_info",
    )
    safe_write_tools = (
        "dingtalk_approval_start", "dingtalk_approval_comment",
        "dingtalk_todo_create", "dingtalk_todo_update",
        "dingtalk_calendar_create_event", "dingtalk_calendar_update_event",
        "dingtalk_create_group", "dingtalk_group_add_members",
        "dingtalk_group_set_notice", "dingtalk_announcement_create",
        "dingtalk_report_submit", "dingtalk_drive_upload",
        "dingtalk_send_file_single", "dingtalk_send_file_group",
        "dingtalk_conference_create",
        "dingtalk_ai_card_send", "dingtalk_card_instance_update",
        "dingtalk_doc_create", "dingtalk_doc_add_member",
    )
    destructive_tools = (
        "dingtalk_approval_action", "dingtalk_approval_revoke",
        "dingtalk_recall_single", "dingtalk_recall_group",
        "dingtalk_group_remove_members", "dingtalk_announcement_delete",
        "dingtalk_conference_close", "dingtalk_calendar_delete_event",
    )
    for name in read_tools:
        assert tools[name].annotations.read_only_hint is True, name
        assert tools[name].annotations.destructive_hint is False, name
    for name in safe_write_tools:
        assert tools[name].annotations.read_only_hint is False, name
        assert tools[name].annotations.destructive_hint is False, name
    for name in destructive_tools:
        assert tools[name].annotations.read_only_hint is False, name
        assert tools[name].annotations.destructive_hint is True, name
    assert len(read_tools) + len(safe_write_tools) + len(destructive_tools) == 46
