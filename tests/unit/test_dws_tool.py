"""agent DWS 工具：只走 market、不接触 keychain。"""

import json

import pytest

from tools import dws as dws_module
from tools.dws import DwsSchemaTool, DwsTool


class _Resp:
    status_code = 200
    text = "ok"

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, *args, **kwargs):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        return _Resp({"success": True, "data": {"services": ["chat"]}})

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        return _Resp({"result": {"output": {"success": True, "data": [1]}}})


async def _token():
    return "tok", None


async def _workspace(base, token):
    return {"authorized": True, "page_url": "https://example/market/capabilities/x?tab=config"}


async def test_write_requires_confirm_without_network(monkeypatch):
    monkeypatch.setattr(dws_module, "_base_or_error", lambda: ("http://market", None))
    monkeypatch.setattr(dws_module, "_auth_token", _token)
    tool = DwsTool()
    out = json.loads(await tool.execute(command="calendar event create", mode="write"))
    assert out["success"] is False
    assert out["error"] == "dws_confirmation_required"


async def test_read_calls_market(monkeypatch):
    monkeypatch.setattr(dws_module, "_base_or_error", lambda: ("http://market", None))
    monkeypatch.setattr(dws_module, "_auth_token", _token)
    monkeypatch.setattr(dws_module, "_workspace", _workspace)
    monkeypatch.setattr(dws_module.httpx, "AsyncClient", _Client)
    tool = DwsTool()
    out = json.loads(await tool.execute(command="calendar event list", mode="read"))
    assert out["success"] is True
    assert out["result"]["data"] == [1]


async def test_missing_token_is_fail_closed(monkeypatch):
    async def no_token():
        return "", "请先登录 AI 平台完成身份授权"

    monkeypatch.setattr(dws_module, "_base_or_error", lambda: ("http://market", None))
    monkeypatch.setattr(dws_module, "_auth_token", no_token)
    tool = DwsTool()
    out = json.loads(await tool.execute(command="calendar event list", mode="read"))
    assert out["success"] is False
    assert "登录" in out["error"]


async def test_schema_tool(monkeypatch):
    monkeypatch.setattr(dws_module, "_base_or_error", lambda: ("http://market", None))
    monkeypatch.setattr(dws_module, "_auth_token", _token)
    monkeypatch.setattr(dws_module, "_workspace", _workspace)
    monkeypatch.setattr(dws_module.httpx, "AsyncClient", _Client)
    tool = DwsSchemaTool()
    out = json.loads(await tool.execute(path="chat"))
    assert out["success"] is True
    assert out["data"]["success"] is True
