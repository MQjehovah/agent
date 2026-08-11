"""Tests for multi-cloud base URL resolution in cloud_common."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
MCP_SRC = ROOT / "mcp_server" / "src"
sys.path.insert(0, str(MCP_SRC))


@pytest.fixture()
def cloud(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUD_BIND_STATE_PATH", str(tmp_path / "bind.json"))
    monkeypatch.delenv("TICKET_API_BASE_URLS", raising=False)
    monkeypatch.delenv("CLOUD_API_BASE_URLS", raising=False)
    # Re-import fresh
    for name in list(sys.modules):
        if name == "cloud_common" or name.startswith("cloud_common."):
            del sys.modules[name]
    import cloud_common as mod

    return mod


def test_configured_base_urls_single(cloud, monkeypatch):
    monkeypatch.setenv("TICKET_API_BASE_URL", "https://only.example.com")
    # module already loaded API_BASE_URL — configured falls back to module const unless URLS set
    monkeypatch.setenv("TICKET_API_BASE_URLS", "")
    assert len(cloud.configured_base_urls()) >= 1


def test_configured_base_urls_multi(cloud, monkeypatch):
    monkeypatch.setenv(
        "TICKET_API_BASE_URLS",
        "https://a.example.com, https://b.example.com/",
    )
    assert cloud.configured_base_urls() == [
        "https://a.example.com",
        "https://b.example.com",
    ]


def test_resolve_caches_and_persists(cloud, monkeypatch, tmp_path):
    monkeypatch.setenv(
        "TICKET_API_BASE_URLS",
        "https://miss.example.com,https://hit.example.com",
    )
    bind_path = tmp_path / "bind.json"
    monkeypatch.setenv("CLOUD_BIND_STATE_PATH", str(bind_path))
    cloud.BIND_STATE_PATH = str(bind_path)

    def fake_request(method, url, **kwargs):
        resp = MagicMock()
        if "hit.example.com" in url:
            resp.status_code = 200
            resp.text = '{"success":true,"data":{"id":"1","code":"SH"}}'
            resp.json.return_value = {
                "success": True,
                "data": {"id": "1", "code": "SH"},
            }
            resp.raise_for_status = MagicMock()
        else:
            resp.status_code = 200
            resp.text = '{"success":false,"data":null}'
            resp.json.return_value = {"success": False, "data": None}
            resp.raise_for_status = MagicMock()
        return resp

    with patch.object(cloud, "auto_login"), patch.object(
        cloud, "request_with_reauth", side_effect=fake_request
    ):
        base = cloud.resolve_base_for_ticket("215000")
    assert base == "https://hit.example.com"
    assert cloud.get_base_url() == "https://hit.example.com"
    assert json.loads(bind_path.read_text(encoding="utf-8"))["baseUrl"] == (
        "https://hit.example.com"
    )
    # cache hit
    with patch.object(cloud, "request_with_reauth") as req:
        assert cloud.resolve_base_for_ticket("215000") == "https://hit.example.com"
        req.assert_not_called()


def test_bind_ticket_cloud_not_found(cloud, monkeypatch):
    monkeypatch.setenv(
        "TICKET_API_BASE_URLS",
        "https://a.example.com,https://b.example.com",
    )

    def fake_request(method, url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '{"success":false,"data":null}'
        resp.json.return_value = {"success": False, "data": None}
        resp.raise_for_status = MagicMock()
        return resp

    with patch.object(cloud, "auto_login"), patch.object(
        cloud, "request_with_reauth", side_effect=fake_request
    ):
        out = cloud.bind_ticket_cloud("999")
    assert out["success"] is False
    assert "tried" in out
