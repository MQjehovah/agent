"""钉钉 MCP 503 硬化 + 审批接口旧版回退测试。

覆盖:
- _api: 503→503→200 退避重试成功(0.5s/1s); 持续 503 文案含权限申请提示; 非 503 不重试;
- dingtalk_approval_tasks: 主 503 时旧版 listids 回退 —— 权限错误(scope+申请链接)、
  回退成功(实例 ID)、回退失败(两个错误都在)、status=1 不回退;
- 权限文案解析: scope 提取正则、无 scope/无链接的容错。
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
# conftest 的 _init_settings 依赖 agent 的 src 在 sys.path 上; 本文件可独立运行
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


def _patch_api_sequence(monkeypatch, module, responses):
    calls, sleeps = [], []
    queue = list(responses)
    monkeypatch.setattr(module.requests, "request",
                        lambda method, url, **kwargs: (calls.append(url), queue.pop(0))[1])
    monkeypatch.setattr(module, "_get_access_token", lambda: "tok")
    monkeypatch.setattr(module.time, "sleep", lambda seconds: sleeps.append(seconds))
    return calls, sleeps


def _patch_main_api_503(monkeypatch, module):
    def fake_api(method, path, **kwargs):
        raise RuntimeError("HTTP 503: code=ServiceUnavailable（钉钉临时故障）")
    monkeypatch.setattr(module, "_api", fake_api)


def _patch_legacy(monkeypatch, module, response):
    calls = []

    def fake_post(url, json=None, timeout=10):
        calls.append({"url": url, "json": json})
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(module.requests, "post", fake_post)
    return calls


def _patch_config(monkeypatch, module):
    monkeypatch.setattr(module, "APP_KEY", "test-key")
    monkeypatch.setattr(module, "APP_SECRET", "test-secret")


# ===== 1. _api 503 重试 =====

def test_api_retries_503_then_succeeds(monkeypatch):
    module = _load()
    calls, sleeps = _patch_api_sequence(monkeypatch, module, [
        FakeResponse(503, {"code": "ServiceUnavailable"}),
        FakeResponse(503, {"code": "ServiceUnavailable"}),
        FakeResponse(200, {"ok": True}),
    ])
    assert module._api("GET", "/v1.0/workflow/workRecords/todoTasks") == {"ok": True}
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]


def test_api_persistent_503_raises_with_permission_hint(monkeypatch):
    module = _load()
    calls, sleeps = _patch_api_sequence(monkeypatch, module, [
        FakeResponse(503, {"code": "ServiceUnavailable"}) for _ in range(3)
    ])
    with pytest.raises(RuntimeError) as excinfo:
        module._api("GET", "/v1.0/workflow/workRecords/todoTasks")
    message = str(excinfo.value)
    assert "HTTP 503" in message
    assert "权限管理" in message
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]


def test_api_non_503_does_not_retry(monkeypatch):
    module = _load()
    calls, sleeps = _patch_api_sequence(monkeypatch, module, [
        FakeResponse(500, {"code": "InternalError", "message": "boom"}),
    ])
    with pytest.raises(RuntimeError) as excinfo:
        module._api("GET", "/v1.0/report/templates")
    assert "HTTP 500" in str(excinfo.value)
    assert len(calls) == 1
    assert sleeps == []


# ===== 2. 审批待办旧版回退 =====

def test_approval_tasks_fallback_permission_error(monkeypatch):
    module = _load()
    _patch_config(monkeypatch, module)
    _patch_main_api_503(monkeypatch, module)
    monkeypatch.setattr(module, "_get_access_token", lambda: "tok")
    legacy_calls = _patch_legacy(monkeypatch, module, FakeResponse(200, {
        "errcode": 88,
        "errmsg": "dingtalk oapi error",
        "sub_code": "60011",
        "sub_msg": ("no permission to use this api, scope=qyapi_aflow "
                    "https://open-dev.dingtalk.com/appscope/apply?content=qyapi_aflow"),
    }))

    payload = json.loads(module.dingtalk_approval_tasks("user1"))
    assert payload["success"] is False
    assert "qyapi_aflow" in payload["error"]
    assert "权限管理" in payload["error"]
    assert "https://open-dev.dingtalk.com/appscope/apply?content=qyapi_aflow" in payload["error"]
    assert legacy_calls[0]["url"].startswith(
        "https://oapi.dingtalk.com/topapi/processinstance/listids")
    assert "access_token=tok" in legacy_calls[0]["url"]
    assert legacy_calls[0]["json"] == {"userid": "user1", "status_list": "RUNNING"}


def test_approval_tasks_fallback_success(monkeypatch):
    module = _load()
    _patch_config(monkeypatch, module)
    _patch_main_api_503(monkeypatch, module)
    monkeypatch.setattr(module, "_get_access_token", lambda: "tok")
    _patch_legacy(monkeypatch, module, FakeResponse(200, {
        "errcode": 0, "result": {"list": ["PI-1", "PI-2"]},
    }))

    payload = json.loads(module.dingtalk_approval_tasks("user1"))
    assert payload["success"] is True
    assert payload["source"] == "legacy"
    assert payload["instance_ids"] == ["PI-1", "PI-2"]
    assert "dingtalk_approval_instance" in payload["note"]


def test_approval_tasks_fallback_failure_keeps_both_errors(monkeypatch):
    module = _load()
    _patch_config(monkeypatch, module)
    _patch_main_api_503(monkeypatch, module)
    monkeypatch.setattr(module, "_get_access_token", lambda: "tok")
    _patch_legacy(monkeypatch, module, FakeResponse(200, {
        "errcode": 500, "errmsg": "system error",
    }))

    payload = json.loads(module.dingtalk_approval_tasks("user1"))
    assert payload["success"] is False
    assert "HTTP 503" in payload["error"]
    assert "旧版审批接口失败" in payload["error"]
    assert "errcode=500" in payload["error"]


def test_approval_tasks_fallback_request_exception_keeps_both_errors(monkeypatch):
    module = _load()
    _patch_config(monkeypatch, module)
    _patch_main_api_503(monkeypatch, module)
    monkeypatch.setattr(module, "_get_access_token", lambda: "tok")
    _patch_legacy(monkeypatch, module, RuntimeError("connection reset"))

    payload = json.loads(module.dingtalk_approval_tasks("user1"))
    assert payload["success"] is False
    assert "HTTP 503" in payload["error"]
    assert "connection reset" in payload["error"]


def test_approval_tasks_status_done_503_no_fallback(monkeypatch):
    module = _load()
    _patch_config(monkeypatch, module)
    _patch_main_api_503(monkeypatch, module)
    monkeypatch.setattr(module, "_get_access_token", lambda: "tok")
    legacy_calls = _patch_legacy(monkeypatch, module, FakeResponse(200, {"errcode": 0}))

    payload = json.loads(module.dingtalk_approval_tasks("user1", status=1))
    assert payload["success"] is False
    assert "HTTP 503" in payload["error"]
    assert "暂不支持回退" in payload["note"]
    assert legacy_calls == []


def test_approval_tasks_non_503_does_not_fallback(monkeypatch):
    module = _load()
    _patch_config(monkeypatch, module)

    def fake_api(method, path, **kwargs):
        raise RuntimeError("HTTP 403: code=Forbidden.AccessDenied message=无权限")
    monkeypatch.setattr(module, "_api", fake_api)
    legacy_calls = _patch_legacy(monkeypatch, module, FakeResponse(200, {"errcode": 0}))

    payload = json.loads(module.dingtalk_approval_tasks("user1"))
    assert payload["success"] is False
    assert "HTTP 403" in payload["error"]
    assert legacy_calls == []


# ===== 3. 权限文案解析 =====

def test_permission_apply_hint_extracts_scope_and_link():
    module = _load()
    hint = module._permission_apply_hint({
        "errcode": 88, "sub_code": "60011",
        "sub_msg": ("no permission to use this api, please apply scope qyapi_aflow "
                    "详情见 https://open-dev.dingtalk.com/appscope/apply?content=abc&x=1"),
    })
    assert hint.startswith("应用缺少钉钉权限 [qyapi_aflow]（OA审批）。")
    assert "权限管理" in hint
    assert hint.endswith("申请链接: https://open-dev.dingtalk.com/appscope/apply?content=abc&x=1")


def test_permission_apply_hint_without_scope_or_link():
    module = _load()
    hint = module._permission_apply_hint({"sub_msg": "no permission"})
    assert "应用缺少钉钉权限（OA审批）" in hint
    assert "申请链接" not in hint
