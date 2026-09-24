"""Gerrit/GitLab/Jira 自研 MCP servers 进程内测试。

- 三份 server 完全自包含(零共享模块), 测试进程内 `Client(module.mcp)` 锁工具面与注解;
- 全部 HTTP 经 monkeypatch 的假会话(module._session.request/get/post), 零真实网络;
- 覆盖: 鉴权选择(PAT/Bearer/Basic/匿名/GitLab 会话登录)、XSSI 剥离、project/change id 编码、
  分页钳制、diff 过滤与截断、错误脱敏、写工具请求体。
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
MCP_SRC = ROOT / "mcp_server" / "src"
for path in (SRC, MCP_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mcp import Client  # noqa: E402


def _load(name):
    return importlib.import_module(name)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        if text:
            self.text = text
        elif payload is not None:
            self.text = json.dumps(payload, ensure_ascii=False)
        else:
            self.text = ""
        self.content = self.text.encode("utf-8") if self.text else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class FakeHTTP:
    """把 requests.Session 的 request/get/post 统一路由到 handler(method, url, kwargs)。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.handler(method, url, kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


def _mount(monkeypatch, module, handler):
    http = FakeHTTP(handler)
    monkeypatch.setattr(module._session, "request", http.request)
    monkeypatch.setattr(module._session, "get", http.get)
    monkeypatch.setattr(module._session, "post", http.post)
    return http


def _annotations(tool):
    return tool.annotations


def _is_read(tool):
    return bool(getattr(tool.annotations, "read_only_hint", False))


def _is_destructive(tool):
    return bool(getattr(tool.annotations, "destructive_hint", False))


GITLAB_READ_TOOLS = {
    "gitlab_whoami",
    "gitlab_list_projects",
    "gitlab_get_project",
    "gitlab_list_branches",
    "gitlab_list_merge_requests",
    "gitlab_get_merge_request",
    "gitlab_get_mr_changes",
    "gitlab_list_mr_notes",
    "gitlab_list_issues",
    "gitlab_get_issue",
    "gitlab_list_commits",
    "gitlab_get_commit_diff",
    "gitlab_get_file",
    "gitlab_list_pipelines",
    "gitlab_get_pipeline",
    "gitlab_get_job_log",
}
GITLAB_WRITE_TOOLS = {
    "gitlab_create_merge_request",
    "gitlab_update_merge_request",
    "gitlab_comment_mr",
    "gitlab_approve_mr",
    "gitlab_create_issue",
    "gitlab_update_issue",
    "gitlab_run_pipeline",
    "gitlab_retry_pipeline",
}
GITLAB_DESTRUCTIVE_TOOLS = {"gitlab_merge_mr", "gitlab_cancel_pipeline"}

GERRIT_READ_TOOLS = {
    "gerrit_query_changes",
    "gerrit_get_change",
    "gerrit_list_files",
    "gerrit_get_file_diff",
    "gerrit_get_commit_message",
    "gerrit_list_comments",
    "gerrit_related_changes",
}
GERRIT_WRITE_TOOLS = {"gerrit_add_reviewer", "gerrit_set_topic", "gerrit_set_wip", "gerrit_restore_change"}
GERRIT_DESTRUCTIVE_TOOLS = {"gerrit_set_review", "gerrit_submit_change", "gerrit_abandon_change"}

JIRA_READ_TOOLS = {
    "jira_server_info",
    "jira_search",
    "jira_get_issue",
    "jira_list_projects",
    "jira_get_create_meta",
    "jira_list_transitions",
    "jira_search_users",
}
JIRA_WRITE_TOOLS = {
    "jira_create_issue",
    "jira_update_issue",
    "jira_add_comment",
    "jira_transition_issue",
    "jira_assign_issue",
}
JIRA_DESTRUCTIVE_TOOLS = {"jira_delete_issue"}


async def _list_tools(module_name):
    module = _load(module_name)
    async with Client(module.mcp, raise_exceptions=True) as client:
        result = await client.list_tools()
    return {tool.name: tool for tool in result.tools}


def _assert_surface(tools, read_tools, write_tools, destructive_tools):
    assert set(tools) == read_tools | write_tools | destructive_tools
    for name in read_tools:
        assert _is_read(tools[name]), name
        assert not _is_destructive(tools[name]), name
    for name in write_tools:
        assert not _is_read(tools[name]), name
        assert not _is_destructive(tools[name]), name
    for name in destructive_tools:
        assert not _is_read(tools[name]), name
        assert _is_destructive(tools[name]), name


async def test_gitlab_tool_surface_and_annotations():
    tools = await _list_tools("gitlab")
    assert len(tools) == 26
    _assert_surface(tools, GITLAB_READ_TOOLS, GITLAB_WRITE_TOOLS, GITLAB_DESTRUCTIVE_TOOLS)


async def test_gerrit_tool_surface_and_annotations():
    tools = await _list_tools("gerrit")
    assert len(tools) == 14
    _assert_surface(tools, GERRIT_READ_TOOLS, GERRIT_WRITE_TOOLS, GERRIT_DESTRUCTIVE_TOOLS)


async def test_jira_tool_surface_and_annotations():
    tools = await _list_tools("jira")
    assert len(tools) == 13
    _assert_surface(tools, JIRA_READ_TOOLS, JIRA_WRITE_TOOLS, JIRA_DESTRUCTIVE_TOOLS)


# ---------------------------------------------------------------- GitLab

def test_gitlab_helpers_project_encoding():
    module = _load("gitlab")
    assert module._encode_project("cloud/xz-data") == "cloud%2Fxz-data"
    assert module._encode_project("230") == "230"
    try:
        module._encode_project("  ")
    except module.GitLabError as exc:
        assert "project" in str(exc)
    else:
        raise AssertionError("空 project 应报错")


def test_gitlab_whoami_uses_pat_and_encoded_project(monkeypatch):
    module = _load("gitlab")
    monkeypatch.setattr(module, "GITLAB_TOKEN", "glpat-test")
    monkeypatch.setattr(module, "GITLAB_PASSWORD", "")
    monkeypatch.setattr(module, "_logged_in", True)
    http = _mount(
        monkeypatch,
        module,
        lambda method, url, kwargs: FakeResponse(
            200, {"id": 7, "path_with_namespace": "cloud/xz-data", "default_branch": "master"}
        ),
    )
    result = module.gitlab_get_project("cloud/xz-data")
    assert result["ok"] is True
    assert http.calls[0]["url"].endswith("/api/v4/projects/cloud%2Fxz-data")
    assert http.calls[0]["headers"]["PRIVATE-TOKEN"] == "glpat-test"
    assert module.gitlab_whoami()["ok"] is True


def test_gitlab_ldap_session_login_flow_and_csrf(monkeypatch):
    module = _load("gitlab")
    monkeypatch.setattr(module, "GITLAB_TOKEN", "")
    monkeypatch.setattr(module, "GITLAB_PASSWORD", "s3cret-pw")
    monkeypatch.setattr(module, "_logged_in", False)
    monkeypatch.setattr(module, "_csrf_token", "")

    def handler(method, url, kwargs):
        if url.endswith("/users/sign_in"):
            return FakeResponse(200, text='<input name="authenticity_token" value="tok123">')
        if url.endswith("/users/auth/ldapmain/callback"):
            return FakeResponse(200, text="ok")
        if url.endswith("/api/v4/user"):
            return FakeResponse(200, {"id": 1, "username": "s_software"})
        if url.endswith("/api/v4/projects/x/issues"):
            return FakeResponse(201, {"iid": 9, "state": "opened", "web_url": "http://gitlab/x/-/issues/9"})
        return FakeResponse(200, text='<meta name="csrf-token" content="csrf456">')

    http = _mount(monkeypatch, module, handler)
    assert module.gitlab_whoami()["ok"] is True
    callback = [c for c in http.calls if c["url"].endswith("/users/auth/ldapmain/callback")][0]
    assert callback["method"] == "POST"
    assert callback["data"]["username"] == module.GITLAB_USERNAME
    assert callback["data"]["password"] == "s3cret-pw"
    assert callback["data"]["authenticity_token"] == "tok123"

    created = module.gitlab_create_issue("x", "标题")
    assert created["ok"] is True
    post = [c for c in http.calls if c["method"] == "POST" and "/api/v4/projects/x/issues" in c["url"]][0]
    assert post["headers"]["X-CSRF-Token"] == "csrf456"
    assert "PRIVATE-TOKEN" not in post["headers"]


def test_gitlab_error_message_redacts_credentials(monkeypatch):
    module = _load("gitlab")
    monkeypatch.setattr(module, "GITLAB_TOKEN", "glpat-secret")
    monkeypatch.setattr(module, "GITLAB_PASSWORD", "")
    monkeypatch.setattr(module, "_logged_in", True)
    _mount(monkeypatch, module, lambda method, url, kwargs: FakeResponse(401, {"message": "401 Unauthorized"}))
    result = module.gitlab_whoami()
    assert result["ok"] is False
    assert "401" in result["error"]
    assert "glpat-secret" not in result["error"]


def test_gitlab_mr_changes_file_filter_and_truncation(monkeypatch):
    module = _load("gitlab")
    monkeypatch.setattr(module, "GITLAB_TOKEN", "t")
    monkeypatch.setattr(module, "_logged_in", True)
    payload = {
        "changes": [
            {"old_path": "a.py", "new_path": "a.py", "diff": "x" * 600},
            {"old_path": "b.py", "new_path": "b.py", "diff": "y" * 600},
        ]
    }
    _mount(monkeypatch, module, lambda method, url, kwargs: FakeResponse(200, payload))
    # max_diff_chars 下限 500: 传 10 会被钳到 500
    result = module.gitlab_get_mr_changes("cloud/x", 3, file="b.py", max_diff_chars=10)
    assert result["ok"] is True
    assert result["file_count"] == 1
    assert result["files"][0]["new_path"] == "b.py"
    assert result["files"][0]["diff"] == "y" * 500
    assert result["files"][0]["diff_truncated"] is True

    listing = module.gitlab_get_mr_changes("cloud/x", 3, include_diff=False)
    assert "diff" not in listing["files"][0]
    assert listing["file_count"] == 2

    missing = module.gitlab_get_mr_changes("cloud/x", 3, file="nope.py")
    assert missing["ok"] is False and "nope.py" in missing["error"]


def test_gitlab_inline_comment_uses_diff_refs(monkeypatch):
    module = _load("gitlab")
    monkeypatch.setattr(module, "GITLAB_TOKEN", "t")
    monkeypatch.setattr(module, "_logged_in", True)

    def handler(method, url, kwargs):
        if url.endswith("/discussions"):
            return FakeResponse(201, {"id": "disc-1"})
        return FakeResponse(200, {"diff_refs": {"base_sha": "b", "head_sha": "h", "start_sha": "s"}})

    http = _mount(monkeypatch, module, handler)
    result = module.gitlab_comment_mr("cloud/x", 7, "这里有问题", file="src/a.py", line=12)
    assert result["ok"] is True and result["inline"] is True
    call = http.calls[-1]
    assert call["method"] == "POST" and call["url"].endswith("/merge_requests/7/discussions")
    position = call["json"]["position"]
    assert position == {
        "base_sha": "b",
        "head_sha": "h",
        "start_sha": "s",
        "position_type": "text",
        "new_path": "src/a.py",
        "new_line": 12,
    }


# ---------------------------------------------------------------- Gerrit

def test_gerrit_helpers_change_id_encoding():
    module = _load("gerrit")
    assert module._encode_change_id("23824") == "23824"
    encoded = module._encode_change_id("cloud/xz~master~I0123abc")
    assert encoded == "cloud%2Fxz~master~I0123abc"
    assert module._strip_xssi(")]}'\n[1]") == "[1]"


def test_gerrit_authenticated_prefix_query_params(monkeypatch):
    module = _load("gerrit")
    monkeypatch.setattr(module, "GERRIT_HTTP_PASSWORD", "http-pass")
    http = _mount(monkeypatch, module, lambda method, url, kwargs: FakeResponse(200, payload=[]))
    result = module.gerrit_query_changes("status:open project:cloud/x", limit=5, start=10)
    assert result["ok"] is True
    call = http.calls[0]
    assert call["url"] == f"{module._base_url()}/a/changes/"
    assert call["auth"] == (module.GERRIT_USERNAME, "http-pass")
    pairs = dict(call["params"])
    assert pairs["q"] == "status:open project:cloud/x"
    assert pairs["n"] == 5 and pairs["S"] == 10


def test_gerrit_anonymous_read_and_write_guard(monkeypatch):
    module = _load("gerrit")
    monkeypatch.setattr(module, "GERRIT_HTTP_PASSWORD", "")
    http = _mount(monkeypatch, module, lambda method, url, kwargs: FakeResponse(200, payload=[]))
    assert module.gerrit_query_changes("status:open")["ok"] is True
    assert "/a/" not in http.calls[0]["url"]
    assert http.calls[0]["auth"] is None
    denied = module.gerrit_set_wip("1")
    assert denied["ok"] is False and "GERRIT_HTTP_PASSWORD" in denied["error"]


def test_gerrit_strips_xssi_prefix(monkeypatch):
    module = _load("gerrit")
    monkeypatch.setattr(module, "GERRIT_HTTP_PASSWORD", "")
    _mount(
        monkeypatch,
        module,
        lambda method, url, kwargs: FakeResponse(200, text=")]}'\n[{\"_number\": 1, \"project\": \"p\"}]"),
    )
    result = module.gerrit_query_changes("status:open")
    assert result["ok"] is True
    assert result["changes"][0]["number"] == 1


def test_gerrit_exclude_patterns(monkeypatch):
    module = _load("gerrit")
    monkeypatch.setattr(module, "GERRIT_HTTP_PASSWORD", "")
    files_payload = {
        "/COMMIT_MSG": {"status": "A"},
        "src/a.py": {"status": "M", "lines_inserted": 3},
        "package-lock.json": {"status": "M"},
        "assets/logo.png": {"status": "A"},
    }
    http = _mount(monkeypatch, module, lambda method, url, kwargs: FakeResponse(200, payload=files_payload))
    result = module.gerrit_list_files("123")
    assert [f["path"] for f in result["files"]] == ["src/a.py"]
    assert sorted(result["excluded"]) == ["assets/logo.png", "package-lock.json"]

    excluded = module.gerrit_get_file_diff("123", "assets/logo.png")
    assert excluded["excluded"] is True
    assert len(http.calls) == 1


def test_gerrit_set_review_payload(monkeypatch):
    module = _load("gerrit")
    monkeypatch.setattr(module, "GERRIT_HTTP_PASSWORD", "pw")
    http = _mount(monkeypatch, module, lambda method, url, kwargs: FakeResponse(200, payload={}))
    result = module.gerrit_set_review(
        "123",
        message="整体没问题",
        labels={"Code-Review": 2},
        inline_comments=[{"file": "src/a.py", "line": 42, "message": "这里改下"}],
        notify="owner",
    )
    assert result["ok"] is True
    payload = http.calls[0]["json"]
    assert http.calls[0]["url"].endswith("/a/changes/123/review")
    assert payload["labels"] == {"Code-Review": 2}
    assert payload["comments"] == {"src/a.py": [{"message": "这里改下", "line": 42}]}
    assert payload["notify"] == "OWNER"
    empty = module.gerrit_set_review("123")
    assert empty["ok"] is False


def test_gerrit_error_includes_status(monkeypatch):
    module = _load("gerrit")
    monkeypatch.setattr(module, "GERRIT_HTTP_PASSWORD", "")
    _mount(monkeypatch, module, lambda method, url, kwargs: FakeResponse(404, text="Not found: 999"))
    result = module.gerrit_get_change("999")
    assert result["ok"] is False and "404" in result["error"]


# ---------------------------------------------------------------- Jira

def test_jira_auth_switches_bearer_and_basic(monkeypatch):
    module = _load("jira")
    monkeypatch.setattr(module, "JIRA_TOKEN", "jira-pat")
    monkeypatch.setattr(module, "JIRA_PASSWORD", "")
    http = _mount(monkeypatch, module, lambda method, url, kwargs: FakeResponse(200, payload={"version": "9.4"}))
    module.jira_server_info()
    call = http.calls[0]
    assert call["headers"]["Authorization"] == "Bearer jira-pat"
    assert call["auth"] is None

    monkeypatch.setattr(module, "JIRA_TOKEN", "")
    monkeypatch.setattr(module, "JIRA_PASSWORD", "jira-pw")
    http2 = _mount(monkeypatch, module, lambda method, url, kwargs: FakeResponse(200, payload={"issues": []}))
    module.jira_search("project = P")
    assert http2.calls[0]["auth"] == (module.JIRA_USERNAME, "jira-pw")
    assert "Authorization" not in http2.calls[0]["headers"]


def test_jira_search_jql_params_and_slim(monkeypatch):
    module = _load("jira")
    monkeypatch.setattr(module, "JIRA_TOKEN", "t")
    payload = {
        "total": 120,
        "issues": [
            {
                "key": "P-1",
                "id": "1",
                "fields": {
                    "summary": "登录失败",
                    "status": {"name": "Open"},
                    "assignee": {"displayName": "张三"},
                    "labels": ["bug"],
                },
            }
        ],
    }
    http = _mount(monkeypatch, module, lambda method, url, kwargs: FakeResponse(200, payload=payload))
    result = module.jira_search('project = P AND status = "Open"', start_at=10, max_results=500)
    assert result["ok"] is True
    params = http.calls[0]["params"]
    assert params["jql"] == 'project = P AND status = "Open"'
    assert params["startAt"] == 10
    assert params["maxResults"] == 100
    assert params["fields"]
    assert result["total"] == 120
    assert result["has_more"] is True
    assert result["issues"][0] == {
        "key": "P-1",
        "id": "1",
        "summary": "登录失败",
        "status": "Open",
        "issuetype": None,
        "priority": None,
        "assignee": "张三",
        "reporter": "",
        "labels": ["bug"],
        "resolution": None,
        "created": None,
        "updated": None,
        "project": None,
    }


def test_jira_transition_resolves_name_and_payload(monkeypatch):
    module = _load("jira")
    monkeypatch.setattr(module, "JIRA_TOKEN", "t")

    def handler(method, url, kwargs):
        if method == "GET":
            return FakeResponse(
                200, {"transitions": [{"id": "31", "name": "Done", "to": {"name": "Done"}}]}
            )
        return FakeResponse(204)

    http = _mount(monkeypatch, module, handler)
    result = module.jira_transition_issue("P-1", "done", resolution="Fixed", comment="已修复")
    assert result["ok"] is True and result["transition_id"] == "31"
    post = [c for c in http.calls if c["method"] == "POST"][0]
    assert post["json"]["transition"] == {"id": "31"}
    assert post["json"]["fields"] == {"resolution": {"name": "Fixed"}}
    assert post["json"]["update"]["comment"][0]["add"]["body"] == "已修复"

    bad = module.jira_transition_issue("P-1", "不存在的流转")
    assert bad["ok"] is False and "未找到流转" in bad["error"]


def test_jira_204_updates_and_delete_ok(monkeypatch):
    module = _load("jira")
    monkeypatch.setattr(module, "JIRA_TOKEN", "t")
    http = _mount(monkeypatch, module, lambda method, url, kwargs: FakeResponse(204))
    assert module.jira_update_issue("P-1", summary="新标题")["ok"] is True
    assert http.calls[0]["method"] == "PUT"
    deleted = module.jira_delete_issue("P-1", delete_subtasks=True)
    assert deleted["ok"] is True
    call = http.calls[-1]
    assert call["method"] == "DELETE"
    assert call["params"] == {"deleteSubtasks": "true"}


def test_jira_get_issue_comments_and_user_search_fallback(monkeypatch):
    module = _load("jira")
    monkeypatch.setattr(module, "JIRA_TOKEN", "t")
    issue_payload = {
        "key": "P-2",
        "id": "2",
        "fields": {
            "summary": "s",
            "comment": {"comments": [{"id": "c1", "author": {"name": "u"}, "created": "2026-01-01", "body": "hi"}]},
        },
    }
    _mount(monkeypatch, module, lambda method, url, kwargs: FakeResponse(200, payload=issue_payload))
    detail = module.jira_get_issue("P-2", include_comments=True)
    assert detail["ok"] is True and detail["comments"][0]["body"] == "hi"

    def handler(method, url, kwargs):
        if "/user/search" in url:
            return FakeResponse(404, payload={"errorMessages": ["no user search"]})
        return FakeResponse(200, payload={"users": [{"name": "u1", "displayName": "用户一"}]})

    _mount(monkeypatch, module, handler)
    users = module.jira_search_users("u")
    assert users["ok"] is True and users["users"][0]["name"] == "u1"


# ---------------------------------------------------------------- 公共纯函数

@pytest.mark.parametrize("module_name", ["gitlab", "gerrit", "jira"])
def test_shared_helpers_truncate_and_clamp(module_name):
    module = _load(module_name)
    assert module._truncate("x" * 30, 10) == ("x" * 10, True)
    assert module._truncate("short", 10) == ("short", False)
    assert module._clamp_int("bad", 1, 5, 3) == 3
    assert module._clamp_int(99, 1, 5, 3) == 5
