"""Unit tests for ticket-mode runtime tool guard."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from ticket_completion_gate import deny_tool_in_ticket_mode, is_ticket_mode  # noqa: E402


def _tool(name: str, payload: dict) -> dict:
    return {"role": "tool", "name": name, "content": json.dumps(payload, ensure_ascii=False)}


def test_is_ticket_mode_from_task():
    assert is_ticket_mode("ticket_id=215362") is True
    assert is_ticket_mode("【BMS工单AI处理】xxx") is True
    assert is_ticket_mode("查天气") is False


def test_deny_dingtalk_in_ticket_mode():
    err = deny_tool_in_ticket_mode(
        tool_name="dingtalk_send_text_single",
        tool_args={},
        task="ticket_id=215362",
        messages=[],
        active_skills=[],
    )
    assert err is not None
    assert "钉钉" in err


def test_allow_dingtalk_outside_ticket_mode():
    err = deny_tool_in_ticket_mode(
        tool_name="dingtalk_send_text_single",
        tool_args={},
        task="发个通知",
        messages=[],
        active_skills=[],
    )
    assert err is None


def test_deny_soft_restart_when_evidence_active():
    err = deny_tool_in_ticket_mode(
        tool_name="soft_restart",
        tool_args={"device_id": "x"},
        task="ticket_id=215362",
        messages=[],
        active_skills=["device-evidence-collect"],
    )
    assert err is not None
    assert "soft_restart" in err


def test_deny_set_control_mode_when_evidence_active():
    err = deny_tool_in_ticket_mode(
        tool_name="set_control_mode",
        tool_args={"device_id": "x", "product_id": "XZ-SC50", "mode": 1},
        task="ticket_id=215378",
        messages=[],
        active_skills=["device-evidence-collect"],
    )
    assert err is not None
    assert "set_control_mode" in err


def test_allow_soft_restart_when_offline_skill_only():
    err = deny_tool_in_ticket_mode(
        tool_name="soft_restart",
        tool_args={},
        task="ticket_id=215362",
        messages=[],
        active_skills=["device-offline-recovery"],
    )
    assert err is None


def test_deny_wrong_ticket_comment():
    err = deny_tool_in_ticket_mode(
        tool_name="create_ticket_comment",
        tool_args={"ticket_id": "999", "content": "x"},
        task="ticket_id=215362",
        messages=[],
        active_skills=["ticket-handling"],
    )
    assert err is not None
    assert "其它工单" in err


def test_deny_second_verified_comment():
    messages = [
        _tool(
            "create_ticket_comment",
            {"success": True, "verified": True, "ticketId": "215362"},
        )
    ]
    err = deny_tool_in_ticket_mode(
        tool_name="create_ticket_comment",
        tool_args={"ticket_id": "215362", "content": "补充"},
        task="ticket_id=215362",
        messages=messages,
        active_skills=["ticket-handling"],
    )
    assert err is not None
    assert "双发" in err or "再次" in err


def test_deny_status3_when_comment_asks_field_fix():
    import json as _json

    comment = "根因已定位。无法远程修复，请人工现场跟进。"
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "function": {
                        "name": "create_ticket_comment",
                        "arguments": _json.dumps(
                            {"ticket_id": "215362", "content": comment},
                            ensure_ascii=False,
                        ),
                    }
                }
            ],
        },
        _tool(
            "create_ticket_comment",
            {"success": True, "verified": True, "ticketId": "215362"},
        ),
    ]
    err = deny_tool_in_ticket_mode(
        tool_name="change_ticket_status",
        tool_args={"ticket_id": "215362", "status": 3},
        task="ticket_id=215362",
        messages=messages,
        active_skills=["ticket-handling"],
    )
    assert err is not None
    assert "status=3" in err or "status=6" in err


def _assistant_send(command: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "function": {
                    "name": "send_command",
                    "arguments": json.dumps(
                        {"sn": "200123DC", "command": command},
                        ensure_ascii=False,
                    ),
                }
            }
        ],
    }


def _assistant_shadow() -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "function": {
                    "name": "get_device_shadow",
                    "arguments": json.dumps(
                        {"device_id": "200123DC", "product_id": "XZ-TITAN810"},
                        ensure_ascii=False,
                    ),
                }
            }
        ],
    }


def test_deny_tail_command_in_ticket_mode():
    err = deny_tool_in_ticket_mode(
        tool_name="send_command",
        tool_args={
            "sn": "200123DC",
            "command": "tail -n 50 /userdata/xzrobot/logs/dock/dock.log | grep -i ERROR",
        },
        task="ticket_id=215358",
        messages=[],
        active_skills=["device-evidence-collect"],
    )
    assert err is not None
    assert "tail" in err.lower() or "文件末尾" in err


def test_allow_time_window_grep_with_head():
    err = deny_tool_in_ticket_mode(
        tool_name="send_command",
        tool_args={
            "sn": "200123DC",
            "command": "grep -a '[2026-08-07 18:01:' /userdata/xzrobot/logs/titan_app/_2026-08-07 | head -n 20",
        },
        task="ticket_id=215358",
        messages=[],
        active_skills=["device-evidence-collect"],
    )
    assert err is None


def test_deny_unbounded_log_grep_without_head():
    err = deny_tool_in_ticket_mode(
        tool_name="send_command",
        tool_args={
            "sn": "200123DC",
            "command": "grep -a '[2026-08-07 18:01:' /userdata/xzrobot/logs/titan_app/_2026-08-07",
        },
        task="ticket_id=215358",
        messages=[],
        active_skills=["device-evidence-collect"],
    )
    assert err is not None
    assert "head" in err.lower()


def test_allow_ls_pipe_grep_filename():
    err = deny_tool_in_ticket_mode(
        tool_name="send_command",
        tool_args={
            "sn": "200123DC",
            "command": "ls /userdata/xzrobot/logs/titan_app/ | grep 2026-08-07",
        },
        task="ticket_id=215358",
        messages=[],
        active_skills=["device-evidence-collect"],
    )
    assert err is None


def test_allow_time_window_after_many_ls():
    """找到文件后仍须能做时间窗抽取：不做总条数硬封顶。"""
    messages = [
        _assistant_send(f"ls /tmp/a{i} | grep 2026-08-07") for i in range(8)
    ]
    err = deny_tool_in_ticket_mode(
        tool_name="send_command",
        tool_args={
            "sn": "x",
            "command": (
                "grep -a '[2026-08-07 18:01:' "
                "/userdata/xzrobot/logs/titan_app/_2026-08-07 | head -n 20"
            ),
        },
        task="ticket_id=215358",
        messages=messages,
        active_skills=[],
    )
    assert err is None


def test_deny_same_command_third_time():
    cmd = "ls /userdata/xzrobot/logs/ | grep titan"
    messages = [_assistant_send(cmd), _assistant_send(cmd)]
    err = deny_tool_in_ticket_mode(
        tool_name="send_command",
        tool_args={"sn": "x", "command": cmd},
        task="ticket_id=215358",
        messages=messages,
        active_skills=[],
    )
    assert err is not None
    assert "同一终端命令" in err or "上限" in err


def test_allow_interactive_extract_after_many_ls():
    messages = [
        _assistant_send(f"ls /tmp/b{i} | grep 2026-08-07") for i in range(6)
    ]
    err = deny_tool_in_ticket_mode(
        tool_name="interactive_session",
        tool_args={
            "sn": "x",
            "commands": [
                "grep -a '[2026-08-07 18:01:' /userdata/xzrobot/logs/app/_2026-08-07 | head -n 20",
                "grep -a '[2026-08-07 18:01:' /userdata/xzrobot/logs/driver2/_2026-08-07 | head -n 20",
            ],
        },
        task="ticket_id=215358",
        messages=messages,
        active_skills=[],
    )
    assert err is None


def test_deny_shadow_after_three_calls():
    messages = [_assistant_shadow(), _assistant_shadow(), _assistant_shadow()]
    err = deny_tool_in_ticket_mode(
        tool_name="get_device_shadow",
        tool_args={"device_id": "200123DC", "product_id": "XZ-TITAN810"},
        task="ticket_id=215358",
        messages=messages,
        active_skills=[],
    )
    assert err is not None
    assert "get_device_shadow" in err


def test_allow_connect_after_many_ls():
    """总条数不再硬封顶；仍允许连终端做抽取。"""
    messages = [
        _assistant_send(f"ls /tmp/d{i} | grep 2026-08-07") for i in range(6)
    ]
    err = deny_tool_in_ticket_mode(
        tool_name="connect_terminal",
        tool_args={"sn": "200123DC"},
        task="ticket_id=215358",
        messages=messages,
        active_skills=[],
    )
    assert err is None


def test_build_retry_message_prefers_comment_over_tail():
    from ticket_completion_gate import build_retry_message

    msg = build_retry_message("215358", "缺少 verified 写评")
    assert "写评" in msg
    assert "tail" in msg.lower() or "影子" in msg


def test_deny_sh_code_as_ticket_id():
    err = deny_tool_in_ticket_mode(
        tool_name="create_ticket_comment",
        tool_args={
            "ticket_id": "SH202608100000000005",
            "content": "x",
        },
        task="ticket_id=215367",
        messages=[],
        active_skills=["ticket-handling"],
    )
    assert err is not None
    assert "数字" in err


def test_deny_change_status_with_sh_code():
    err = deny_tool_in_ticket_mode(
        tool_name="change_ticket_status",
        tool_args={"ticket_id": "SH202608100000000005", "status": 6},
        task="ticket_id=215367",
        messages=[],
        active_skills=[],
    )
    assert err is not None
    assert "数字" in err
