"""钉钉 MCP 用户标识换算(工号兜底)测试。

覆盖:
- 纯数字=真钉钉 userId: user/get 快路径命中, 不建通讯录索引;
- 纯数字=工号: user/get 失败 → 通讯录索引(job_number)命中 → 返回真实 userid/unionid;
- 索引缓存命中不重建; 构建失败返回空索引且不抛;
- unionId → userid(getbyunionid); 失败/空值错误文案;
- 审批工具接入: approval_tasks 主路径 params.userId 与旧版回退 listbyuserid
  均使用换算后的真实 userid; approval_action/comment 同样换算;
- get_user_detail 工号兜底重试; _resolve_unionid 工号兜底。
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

JOB = "202202100024"
REAL_UID = "1642483198771392"
UNION = "RKkG4UniPAVxE3eKWwiSY12wiEiE"


def _load():
    return importlib.import_module("dingtalk")


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


def _user_get_route(payload):
    """user/get: 真 userId 成功; 工号失败(模拟 60121)。"""
    uid = str((payload or {}).get("userid") or "")
    if uid == REAL_UID:
        return FakeResponse({"errcode": 0, "result": {
            "userid": REAL_UID, "unionid": UNION, "job_number": JOB, "name": "季明清"}})
    return FakeResponse({"errcode": 60121, "errmsg": "找不到该用户"})


def _user_list_route(payload):
    return FakeResponse({"errcode": 0, "result": {
        "list": [{"userid": REAL_UID, "unionid": UNION, "job_number": JOB, "name": "季明清"}],
        "has_more": False}})


@pytest.fixture
def env(monkeypatch):
    """假 oapi requests.post(按 URL 路由) + 假 _api; 清全部换算/索引缓存。"""
    module = _load()
    module._UNIONID_CACHE.clear()
    module._USERID_CACHE.clear()
    module._directory_index_cache["data"] = None
    module._directory_index_cache["built_at"] = 0.0
    monkeypatch.setattr(module, "APP_KEY", "test-key")
    monkeypatch.setattr(module, "APP_SECRET", "test-secret")
    monkeypatch.setattr(module, "_get_access_token", lambda: "tok")
    state = {
        "posts": [],
        "api": [],
        "api_response": {"result": {"list": [], "nextToken": 0}},
        "routes": {
            "/topapi/v2/user/get": _user_get_route,
            "getbyunionid": lambda payload: FakeResponse(
                {"errcode": 0, "result": {"userid": REAL_UID}}),
            "department/listsub": lambda payload: FakeResponse({"errcode": 0, "result": []}),
            "/topapi/v2/user/list": _user_list_route,
        },
    }

    def fake_post(url, json=None, timeout=10):
        state["posts"].append({"url": url, "json": json})
        for key, route in state["routes"].items():
            if key in url:
                return route(json)
        return FakeResponse({"errcode": 0, "result": {}})

    monkeypatch.setattr(module.requests, "post", fake_post)

    def fake_api(method, path, *, params=None, json_body=None, **kwargs):
        state["api"].append({"method": method, "path": path, "params": params, "json": json_body})
        response = state["api_response"]
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(module, "_api", fake_api)
    return module, state


def _posts(state, key):
    return [p for p in state["posts"] if key in p["url"]]


# ===== 1. _resolve_dingtalk_userid: 快路径 / 工号兜底 / unionId =====

def test_fast_path_real_userid_does_not_build_index(env):
    module, state = env
    userid, unionid, err = module._resolve_dingtalk_userid(REAL_UID)
    assert (userid, unionid, err) == (REAL_UID, UNION, "")
    assert len(_posts(state, "/topapi/v2/user/get")) == 1
    assert _posts(state, "/topapi/v2/user/list") == []   # 未建索引


def test_job_number_falls_back_to_directory_index(env):
    module, state = env
    userid, unionid, err = module._resolve_dingtalk_userid(JOB)
    assert (userid, unionid, err) == (REAL_UID, UNION, "")
    assert _posts(state, "/topapi/v2/user/get")[0]["json"] == {"userid": JOB}
    assert len(_posts(state, "department/listsub")) == 1          # 索引仅构建一次
    assert _posts(state, "/topapi/v2/user/list")[0]["json"]["dept_id"] == 1


def test_resolver_cache_avoids_repeat_calls(env):
    module, state = env
    assert module._resolve_dingtalk_userid(JOB)[0] == REAL_UID
    assert module._resolve_dingtalk_userid(JOB)[0] == REAL_UID
    assert len(_posts(state, "/topapi/v2/user/get")) == 1
    assert len(_posts(state, "department/listsub")) == 1
    assert len(_posts(state, "/topapi/v2/user/list")) == 1


def test_directory_index_cache_hit_no_rebuild(env):
    module, state = env
    assert module._directory_index()["by_job_number"][JOB]["userid"] == REAL_UID
    assert module._directory_index()["by_job_number"][JOB]["userid"] == REAL_UID
    assert len(_posts(state, "department/listsub")) == 1
    assert len(_posts(state, "/topapi/v2/user/list")) == 1


def test_directory_index_build_failure_returns_empty(env):
    module, state = env
    state["routes"]["department/listsub"] = lambda payload: FakeResponse(
        {"errcode": 500, "errmsg": "boom"})
    index = module._directory_index()
    assert index["by_job_number"] == {} and index["by_userid"] == {}


def test_directory_index_paginates_department_users(env):
    """user/list 分页: 按 has_more/next_cursor 翻页收集全部用户。"""
    module, state = env

    def paged(payload):
        cursor = (payload or {}).get("cursor", 0)
        if cursor == 0:
            return FakeResponse({"errcode": 0, "result": {
                "list": [{"userid": "U-1", "unionid": "UN-1", "job_number": "J-1"}],
                "has_more": True, "next_cursor": 7}})
        assert cursor == 7
        return FakeResponse({"errcode": 0, "result": {
            "list": [{"userid": "U-2", "unionid": "UN-2", "job_number": "J-2"}],
            "has_more": False}})

    state["routes"]["/topapi/v2/user/list"] = paged
    index = module._directory_index()
    assert set(index["by_job_number"]) == {"J-1", "J-2"}
    assert index["by_unionid"] == {"UN-1": "U-1", "UN-2": "U-2"}
    assert [p["json"]["cursor"] for p in _posts(state, "/topapi/v2/user/list")] == [0, 7]


def test_job_number_not_found_hint(env):
    module, state = env
    state["routes"]["/topapi/v2/user/list"] = lambda payload: FakeResponse(
        {"errcode": 0, "result": {"list": [], "has_more": False}})
    userid, unionid, err = module._resolve_dingtalk_userid(JOB)
    assert (userid, unionid) == ("", "")
    assert "未找到该钉钉用户" in err and "工号" in err and "钉钉 userId/unionId" in err


def test_union_id_resolves_to_userid(env):
    module, state = env
    userid, unionid, err = module._resolve_dingtalk_userid("UNION-X")
    assert (userid, unionid, err) == (REAL_UID, "UNION-X", "")
    assert _posts(state, "getbyunionid")[0]["json"] == {"unionid": "UNION-X"}


def test_union_id_lookup_failure_returns_raw_with_error(env):
    module, state = env
    state["routes"]["getbyunionid"] = lambda payload: FakeResponse(
        {"errcode": 60121, "errmsg": "找不到该 unionId"})
    userid, unionid, err = module._resolve_dingtalk_userid("UNION-X")
    assert userid == "UNION-X" and unionid == "" and err


def test_resolve_userid_empty(env):
    module, _ = env
    assert module._resolve_dingtalk_userid("") == ("", "", "dingtalk_userid/unionId 不能为空")


def test_resolve_unionid_job_number_fallback(env):
    module, state = env
    assert module._resolve_unionid(JOB) == (UNION, "")
    assert len(_posts(state, "department/listsub")) == 1


# ===== 2. 工具接入 =====

def test_approval_tasks_job_number_uses_real_userid(env):
    module, state = env
    payload = json.loads(module.dingtalk_approval_tasks(JOB))
    assert payload["success"] is True
    assert state["api"][0]["params"]["userId"] == REAL_UID


def test_approval_tasks_job_number_legacy_fallback_uses_real_userid(env):
    module, state = env
    state["api_response"] = RuntimeError("HTTP 503: code=ServiceUnavailable（钉钉临时故障）")
    state["routes"]["/process/listbyuserid"] = lambda payload: FakeResponse(
        {"errcode": 0, "result": {"list": ["PI-1"]}})
    state["routes"]["/processinstance/listids"] = lambda payload: FakeResponse(
        {"errcode": 0, "result": {"list": ["PI-1"]}})

    payload = json.loads(module.dingtalk_approval_tasks(JOB))
    assert payload["success"] is True
    assert payload["source"] == "legacy"
    listby = _posts(state, "/process/listbyuserid")
    assert listby[0]["json"]["userid"] == REAL_UID


def test_approval_action_and_comment_job_number_resolved(env):
    module, state = env
    module.dingtalk_approval_action(task_id=9, result="agree", dingtalk_userid=JOB)
    module.dingtalk_approval_comment(process_instance_id="PI-1", text="ok", dingtalk_userid=JOB)
    assert state["api"][0]["json"]["actionerUserId"] == REAL_UID
    assert state["api"][1]["json"]["commentUserId"] == REAL_UID


def test_get_user_detail_job_number_retries_with_real_userid(env):
    module, state = env
    payload = json.loads(module.dingtalk_get_user_detail(JOB))
    assert payload["success"] is True
    assert payload["user"]["userid"] == REAL_UID
    assert payload["user"]["unionid"] == UNION
    get_calls = _posts(state, "/topapi/v2/user/get")
    assert [c["json"]["userid"] for c in get_calls] == [JOB, REAL_UID]


def test_get_user_detail_unknown_job_number_hint(env):
    module, state = env
    state["routes"]["/topapi/v2/user/list"] = lambda payload: FakeResponse(
        {"errcode": 0, "result": {"list": [], "has_more": False}})
    payload = json.loads(module.dingtalk_get_user_detail(JOB))
    assert payload["success"] is False
    assert "工号" in payload["error"]
