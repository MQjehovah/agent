"""upload_bag_file url/attach_ready helpers."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "mcp_server", "src"))

import remote_operation as ro  # noqa: E402


def test_extract_bag_name_from_data():
    name = ro._extract_bag_name(
        {"data": {"bag_name": "all_2026-08-10-16-59-11_0.bag"}},
        "/opt/xzrobot/bags/other.bag",
    )
    assert name == "all_2026-08-10-16-59-11_0.bag"


def test_extract_bag_name_from_path_fallback():
    name = ro._extract_bag_name({}, "/opt/xzrobot/bags/all_x.bag")
    assert name == "all_x.bag"


def test_upload_bag_file_fallback_upload_list(monkeypatch):
    calls = []

    def fake_post(suffix, device_id, product_id, req_id=0, param=None):
        calls.append(suffix)
        if suffix == "bag/upload":
            return {
                "success": True,
                "returnCode": 200,
                "returnMsg": "ok",
                "data": {"bag_name": "all_x.bag"},
            }
        if suffix == "bag/uploadList":
            return {
                "success": True,
                "returnCode": 200,
                "data": {"url": "https://xz-server.oss-cn-shanghai.aliyuncs.com/all_x.bag"},
            }
        return {"success": False}

    monkeypatch.setattr(ro, "_remote_post", fake_post)
    out = ro.upload_bag_file("10000004", "XZ-SC50", file_path="/opt/xzrobot/bags/all_x.bag")
    assert out["success"] is True
    assert out["url"] == "https://xz-server.oss-cn-shanghai.aliyuncs.com/all_x.bag"
    assert out["attach_ready"] is True
    assert out["fallback"] == "uploadList"
    assert calls == ["bag/upload", "bag/uploadList"]


def test_upload_bag_file_no_url_sets_hint(monkeypatch):
    def fake_post(suffix, device_id, product_id, req_id=0, param=None):
        return {
            "success": True,
            "returnCode": 200,
            "data": {"bag_name": "all_x.bag"},
        }

    monkeypatch.setattr(ro, "_remote_post", fake_post)
    out = ro.upload_bag_file("10000004", "XZ-SC50", file_path="/opt/xzrobot/bags/all_x.bag")
    assert out["url"] is None
    assert out["attach_ready"] is False
    assert "禁止" in (out.get("hint") or "")
