"""Unit tests for ticket comment page verify helpers."""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "mcp_server", "src"))

import ticket_ops as to  # noqa: E402


def test_comment_content_matches_exact_and_prefix():
    assert to._comment_content_matches("hello world", "hello world")
    assert to._comment_content_matches("abc", "xx abc yy")
    long = "A" * 200
    assert to._comment_content_matches(long, long + " trailing")
    assert not to._comment_content_matches("foo", "bar")
    # Old short page comment must NOT match a longer new submission that contains it
    assert not to._comment_content_matches(
        "【AI分析】完整新评论含旧片段",
        "【AI分析】",
    )
    # Short expected still requires equality or being contained in page text
    assert to._comment_content_matches("短评", "前缀 短评 后缀")
    assert not to._comment_content_matches("短评加长内容", "短评")


def test_list_ticket_comments_shapes_records():
    fake_page = {
        "success": True,
        "returnCode": 200,
        "data": {
            "current": 1,
            "size": 10,
            "total": 1,
            "pages": 1,
            "records": [
                {
                    "id": 9,
                    "ticketId": "215361",
                    "content": "【AI诊断】测试评论正文",
                    "createTime": "2026-08-10 09:00:00",
                    "createUserName": "bot",
                }
            ],
        },
    }
    with patch.object(to, "_list_comments", return_value=fake_page):
        out = to.list_ticket_comments("215361", current=1, size=10)
    assert out["success"] is True
    assert out["total"] == 1
    assert out["comments"][0]["id"] == 9
    assert out["comments"][0]["content"].startswith("【AI诊断】")


def test_create_ticket_comment_success_requires_page_verify():
    create_resp = {"success": True, "returnCode": 200, "returnMsg": "ok", "data": {}}
    page_empty = {
        "success": True,
        "returnCode": 200,
        "data": {"total": 0, "records": []},
    }
    page_hit = {
        "success": True,
        "returnCode": 200,
        "data": {
            "total": 1,
            "records": [{"id": 1, "content": "【AI分析】已恢复", "createTime": "t"}],
        },
    }
    with patch.object(to, "_create_comment", return_value=create_resp), patch.object(
        to, "_list_comments", side_effect=[page_empty, page_hit]
    ):
        out = to.create_ticket_comment("215363", "【AI分析】已恢复")
    assert out["apiSuccess"] is True
    assert out["verified"] is True
    assert out["success"] is True
    assert out["matchedComment"]["id"] == 1


def test_create_ticket_comment_api_ok_but_page_empty_is_failure():
    create_resp = {"success": True, "returnCode": 200, "data": {}}
    page_empty = {
        "success": True,
        "returnCode": 200,
        "data": {"total": 0, "records": []},
    }
    with patch.object(to, "_create_comment", return_value=create_resp), patch.object(
        to, "_list_comments", return_value=page_empty
    ):
        out = to.create_ticket_comment("215363", "【AI分析】应落库但未落库")
    assert out["apiSuccess"] is True
    assert out["verified"] is False
    assert out["success"] is False


def test_create_ticket_comment_ambiguous_500_verified_counts_success():
    create_resp = {
        "success": False,
        "returnCode": 500,
        "returnMsg": "服务器业务异常",
        "data": None,
    }
    page_empty = {
        "success": True,
        "returnCode": 200,
        "data": {"total": 0, "records": []},
    }
    page_hit = {
        "success": True,
        "returnCode": 200,
        "data": {
            "total": 1,
            "records": [{"id": 2, "content": "同一条内容", "createTime": "t"}],
        },
    }
    with patch.object(to, "_create_comment", return_value=create_resp), patch.object(
        to, "_list_comments", side_effect=[page_empty, page_hit]
    ):
        out = to.create_ticket_comment("215361", "同一条内容")
    assert out["apiSuccess"] is False
    assert out["verified"] is True
    assert out["success"] is True
    assert "禁止" in (out.get("hint") or "")


def test_create_ticket_comment_skips_when_ai_comment_exists():
    existing_page = {
        "success": True,
        "returnCode": 200,
        "data": {
            "total": 1,
            "records": [
                {
                    "id": 99,
                    "content": "【故障定位】设备雷达串口 Permission denied，无法远程修复。",
                    "createTime": "t1",
                }
            ],
        },
    }
    with patch.object(to, "_list_comments", return_value=existing_page), patch.object(
        to, "_create_comment"
    ) as create_mock:
        out = to.create_ticket_comment(
            "215366",
            "【根因确认】另一条更长的补充评论，不应再写入。",
        )
    create_mock.assert_not_called()
    assert out["success"] is True
    assert out["verified"] is True
    assert out["reused_existing"] is True
    assert out["blocked_new_write"] is True
    assert out["matchedComment"]["id"] == 99
