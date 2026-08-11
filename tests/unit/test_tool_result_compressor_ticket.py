"""Tool result compressor preserves ticket verify fields."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tool_result_compressor import compress_tool_result  # noqa: E402


def test_compress_keeps_verified_and_ticket_id():
    payload = {
        "success": True,
        "verified": True,
        "apiSuccess": True,
        "ticketId": "215362",
        "hint": "禁止再用相同 content 重试",
        "content": "x" * 8000,
        "data": {"huge": "y" * 5000},
    }
    raw = json.dumps(payload, ensure_ascii=False)
    out = compress_tool_result("create_ticket_comment", raw, budget=500)
    data = json.loads(out)
    assert data.get("verified") is True
    assert data.get("ticketId") == "215362"
    assert data.get("apiSuccess") is True
    assert data.get("success") is True
