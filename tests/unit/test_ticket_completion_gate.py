"""Unit tests for ticket comment completion hard gate."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from ticket_completion_gate import (  # noqa: E402
    evaluate_ticket_completion_gate,
    extract_ticket_id_from_text,
    has_closing_status_after_verified_comment,
    has_verified_comment_for_ticket,
    subagent_comment_claim_unverified,
)


def _tool(name: str, payload: dict) -> dict:
    return {"role": "tool", "name": name, "content": json.dumps(payload, ensure_ascii=False)}


def test_extract_ticket_id_from_task():
    assert extract_ticket_id_from_text("ticket_id=215362 请处理") == "215362"
    assert extract_ticket_id_from_text("【BMS工单AI处理】ticket_id=215362") == "215362"


def test_gate_rejects_non_ticket_claim_on_bms_payload():
    task = (
        "【BMS工单AI处理】ticket_id=215378\n"
        "工单编号：SH202608110000000001\n"
        "工单名称：10000004手动模式下无法执行定时任务\n"
        "设备SN：10000004\n"
        "产品：XZ-SC50"
    )
    messages = [
        {"role": "user", "content": task},
        _tool("get_ticket", {"success": True, "id": "215378", "code": "SH202608110000000001"}),
    ]
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="非工单场景，按仅采集模式处理完毕。\n📋 设备: 10000004",
    )
    assert gate.applicable is True
    assert gate.ok is False
    assert "非工单" in gate.reason or "仅采集" in gate.reason


def test_gate_rejects_invented_control_mode_switch():
    task = "【BMS工单AI处理】ticket_id=215378"
    fake = (
        "已远程下发指令将 `control_mode` 切换为 `AUTO`，"
        "当前影子状态已确认生效：control_mode=\"自动模式\"。"
    )
    messages = [
        {"role": "user", "content": task},
        _tool("get_ticket", {"success": True, "id": "215378"}),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {
                        "name": "create_ticket_comment",
                        "arguments": json.dumps(
                            {"ticket_id": "215378", "content": fake},
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        },
        _tool(
            "create_ticket_comment",
            {"success": True, "verified": True, "ticketId": "215378"},
        ),
        _tool(
            "change_ticket_status",
            {"success": True, "ticketId": "215378", "status": 3},
        ),
    ]
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="📋 工单\n💬 评论: 已写回",
    )
    assert gate.ok is False
    assert "set_control_mode" in gate.reason or "编造" in gate.reason


def test_gate_allows_control_mode_claim_with_tool():
    task = "ticket_id=215378"
    body = "已远程下发指令将 control_mode 切换为 AUTO，影子已确认自动模式。"
    messages = [
        {"role": "user", "content": task},
        _tool("set_control_mode", {"success": True, "mode": 1}),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {
                        "name": "create_ticket_comment",
                        "arguments": json.dumps(
                            {"ticket_id": "215378", "content": body},
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        },
        _tool(
            "create_ticket_comment",
            {"success": True, "verified": True, "ticketId": "215378"},
        ),
        _tool(
            "change_ticket_status",
            {"success": True, "ticketId": "215378", "status": 3},
        ),
    ]
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="📋 工单\n💬 评论: 已写回",
    )
    assert gate.ok is True


def test_gate_rejects_missing_comment():
    task = "【BMS工单AI处理】ticket_id=215362"
    messages = [
        {"role": "user", "content": task},
        _tool("get_ticket", {"success": True, "id": "215362", "code": "SH1"}),
    ]
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="📋 工单\n💬 评论: 已写回",
    )
    assert gate.applicable is True
    assert gate.ok is False
    assert gate.ticket_id == "215362"
    assert "verified" in gate.reason or "已写回" in gate.reason


def test_gate_rejects_wrong_ticket_verified():
    task = "ticket_id=215362"
    messages = [
        {"role": "user", "content": task},
        _tool(
            "create_ticket_comment",
            {
                "success": True,
                "verified": True,
                "ticketId": "215363",
                "apiSuccess": True,
            },
        ),
    ]
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="💬 评论: 已写回",
    )
    assert gate.ok is False
    assert not has_verified_comment_for_ticket(messages, "215362")


def test_gate_rejects_verified_without_closing_status():
    task = "ticket_id=215362"
    messages = [
        {"role": "user", "content": task},
        _tool(
            "change_ticket_status",
            {"success": True, "ticketId": "215362", "status": 5},
        ),
        _tool(
            "create_ticket_comment",
            {
                "success": True,
                "verified": True,
                "ticketId": "215362",
            },
        ),
    ]
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="📋 工单: x (id=215362)\n💬 评论: 已写回",
    )
    assert gate.ok is False
    assert "change_ticket_status" in gate.reason
    assert not has_closing_status_after_verified_comment(messages, "215362")


def test_gate_passes_matching_verified_and_status():
    task = "ticket_id=215362"
    messages = [
        {"role": "user", "content": task},
        _tool(
            "create_ticket_comment",
            {
                "success": True,
                "verified": True,
                "ticketId": "215362",
            },
        ),
        _tool(
            "change_ticket_status",
            {"success": True, "ticketId": "215362", "status": 6},
        ),
    ]
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="📋 工单: x (id=215362)\n💬 评论: 已写回",
    )
    assert gate.ok is True
    assert gate.reason == "verified_comment_and_status"


def test_gate_rejects_progress_phrase():
    task = "ticket_id=215362"
    messages = [{"role": "user", "content": task}]
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="正在处理中...",
    )
    assert gate.ok is False
    assert "进度" in gate.reason


def test_gate_allows_honest_failure():
    task = "ticket_id=215362"
    messages = [
        {"role": "user", "content": task},
        _tool(
            "create_ticket_comment",
            {
                "success": False,
                "verified": False,
                "ticketId": "215362",
                "error": "服务器业务异常",
            },
        ),
    ]
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="📋 工单\n💬 评论: 失败（未写回）",
    )
    assert gate.ok is True
    assert gate.reason == "honest_failure"


def test_gate_skips_non_ticket_tasks():
    gate = evaluate_ticket_completion_gate(
        task="查一下天气",
        messages=[{"role": "user", "content": "查一下天气"}],
        final_content="今天晴",
    )
    assert gate.applicable is False
    assert gate.ok is True


def test_subagent_claim_unverified():
    task = "ticket_id=215362"
    assert (
        subagent_comment_claim_unverified(
            task=task,
            result_text="💬 评论: 已写回",
            messages=[],
        )
        is True
    )
    messages = [
        _tool(
            "create_ticket_comment",
            {"verified": True, "ticketId": "215362", "success": True},
        ),
        _tool(
            "change_ticket_status",
            {"success": True, "ticketId": "215362", "status": 3},
        ),
    ]
    assert (
        subagent_comment_claim_unverified(
            task=task,
            result_text="💬 评论: 已写回",
            messages=messages,
        )
        is False
    )


def test_subagent_claim_missing_status_is_unverified():
    task = "ticket_id=215362"
    messages = [
        _tool(
            "create_ticket_comment",
            {"verified": True, "ticketId": "215362", "success": True},
        )
    ]
    assert (
        subagent_comment_claim_unverified(
            task=task,
            result_text="💬 评论: 已写回",
            messages=messages,
        )
        is True
    )


def test_gate_rejects_status3_with_field_suggestion_only():
    """仅诊断+请人工处理却改 3 → 拦截（通用，不限故障码）。"""
    task = "ticket_id=215362"
    comment = (
        "【AI诊断结论】故障已定位。"
        "根因分析见日志摘录。"
        "无法远程修复，请人工现场跟进处理。"
    )
    messages = [
        {"role": "user", "content": task},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {
                        "name": "create_ticket_comment",
                        "arguments": json.dumps(
                            {"ticket_id": "215362", "content": comment},
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        },
        _tool(
            "create_ticket_comment",
            {"success": True, "verified": True, "ticketId": "215362"},
        ),
        _tool(
            "change_ticket_status",
            {"success": True, "ticketId": "215362", "status": 3},
        ),
    ]
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="📋 工单\n💬 评论: 已写回\n状态→3",
    )
    assert gate.ok is False
    assert "status=3" in gate.reason or "请人工" in gate.reason or "请现场" in gate.reason


def test_gate_allows_status6_with_field_suggestion():
    task = "ticket_id=215362"
    comment = "【AI分析】根因已定位，无法远程修复，请人工现场处理。已转问题分析。"
    messages = [
        {"role": "user", "content": task},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {
                        "name": "create_ticket_comment",
                        "arguments": json.dumps(
                            {"ticket_id": "215362", "content": comment},
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        },
        _tool(
            "create_ticket_comment",
            {"success": True, "verified": True, "ticketId": "215362"},
        ),
        _tool(
            "change_ticket_status",
            {"success": True, "ticketId": "215362", "status": 6},
        ),
    ]
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="💬 评论: 已写回",
    )
    assert gate.ok is True
    assert gate.reason == "verified_comment_and_status"


def test_gate_sees_subagent_ticket_evidence():
    """父会话可通过 subagent.ticket_evidence 认定写评+改状态已完成。"""
    task = "【BMS工单AI处理】ticket_id=215366"
    messages = [
        {"role": "user", "content": task},
        _tool(
            "subagent",
            {
                "success": True,
                "status": "completed",
                "result": "💬 评论: 已写回",
                "ticket_evidence": {
                    "create_ticket_comment": {
                        "success": True,
                        "verified": True,
                        "ticketId": "215366",
                    },
                    "change_ticket_status": {
                        "success": True,
                        "ticketId": "215366",
                        "status": 6,
                    },
                },
            },
        ),
    ]
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="📋 工单\n💬 评论: 已写回",
    )
    assert gate.ok is True
    assert gate.reason == "verified_comment_and_status"


def test_gate_parent_tool_unavailable_requires_redispatch():
    from ticket_completion_gate import has_comment_tool_unavailable

    task = "ticket_id=215367"
    messages = [
        {"role": "user", "content": task},
        {
            "role": "tool",
            "name": "create_ticket_comment",
            "content": "工具 create_ticket_comment 不存在",
        },
    ]
    assert has_comment_tool_unavailable(messages) is True
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="已尽力处理。",
    )
    assert gate.applicable is True
    assert gate.ok is False
    assert "不存在" in gate.reason or "不可用" in gate.reason
    assert "设备运维" in gate.retry_message


def test_gate_parent_tool_unavailable_honest_failure():
    task = "ticket_id=215367"
    messages = [
        {"role": "user", "content": task},
        {
            "role": "tool",
            "name": "create_ticket_comment",
            "content": "工具 create_ticket_comment 不存在",
        },
    ]
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="评论失败/未写回：父代理无工单写评工具。",
    )
    assert gate.ok is True
    assert "honest_failure" in gate.reason


def test_gate_blocks_located_bag_without_attachment():
    task = "ticket_id=215372"
    messages = [
        {"role": "user", "content": task},
        _tool(
            "find_bags_near_time",
            {
                "success": True,
                "matched_count": 1,
                "matched": [
                    {
                        "file_name": "all_2026-08-10-16-59-11_0.bag",
                        "file_path": "/opt/xzrobot/bags/all_2026-08-10-16-59-11_0.bag",
                    }
                ],
            },
        ),
        _tool(
            "upload_bag_file",
            {
                "success": True,
                "url": None,
                "attach_ready": False,
                "bag_name": "all_2026-08-10-16-59-11_0.bag",
                "data": {"bag_name": "all_2026-08-10-16-59-11_0.bag"},
            },
        ),
        _tool(
            "create_ticket_attachment",
            {
                "success": True,
                "ticketId": "215372",
                "name": "camera_10000004_前.jpg",
                "url": "https://xz-server.oss-cn-shanghai.aliyuncs.com/camera.jpg",
            },
        ),
        _tool(
            "create_ticket_comment",
            {
                "success": True,
                "verified": True,
                "ticketId": "215372",
            },
        ),
        _tool(
            "change_ticket_status",
            {"success": True, "ticketId": "215372", "status": 6},
        ),
    ]
    # seed comment text via assistant tool_call for extract_verified_comment_texts
    messages.insert(
        -2,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "function": {
                        "name": "create_ticket_comment",
                        "arguments": json.dumps(
                            {
                                "ticket_id": "215372",
                                "content": "已定位录包 all_2026-08-10-16-59-11_0.bag。附件已挂载：相机图。",
                            },
                            ensure_ascii=False,
                        ),
                    }
                }
            ],
        },
    )
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="📋 工单\n💬 评论: 已写回",
    )
    assert gate.ok is False
    assert "录包" in gate.reason or ".bag" in gate.reason


def test_gate_allows_bag_gap_when_comment_admits_no_url():
    task = "ticket_id=215372"
    messages = [
        {"role": "user", "content": task},
        _tool(
            "upload_bag_file",
            {
                "success": True,
                "url": None,
                "attach_ready": False,
                "bag_name": "all_2026-08-10-16-59-11_0.bag",
            },
        ),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "function": {
                        "name": "create_ticket_comment",
                        "arguments": json.dumps(
                            {
                                "ticket_id": "215372",
                                "content": "录包上传未返回 url，请人工挂载 all_2026-08-10-16-59-11_0.bag。已转问题分析。",
                            },
                            ensure_ascii=False,
                        ),
                    }
                }
            ],
        },
        _tool(
            "create_ticket_comment",
            {"success": True, "verified": True, "ticketId": "215372"},
        ),
        _tool(
            "change_ticket_status",
            {"success": True, "ticketId": "215372", "status": 6},
        ),
    ]
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="💬 评论: 已写回",
    )
    assert gate.ok is True


def test_gate_passes_when_bag_attached():
    task = "ticket_id=215372"
    messages = [
        {"role": "user", "content": task},
        _tool(
            "find_bags_near_time",
            {
                "success": True,
                "matched": [{"file_name": "all_2026-08-10-16-59-11_0.bag"}],
            },
        ),
        _tool(
            "create_ticket_attachment",
            {
                "success": True,
                "ticketId": "215372",
                "name": "all_2026-08-10-16-59-11_0.bag",
                "url": "https://xz-server.oss-cn-shanghai.aliyuncs.com/all.bag",
            },
        ),
        _tool(
            "create_ticket_comment",
            {"success": True, "verified": True, "ticketId": "215372"},
        ),
        _tool(
            "change_ticket_status",
            {"success": True, "ticketId": "215372", "status": 6},
        ),
    ]
    gate = evaluate_ticket_completion_gate(
        task=task,
        messages=messages,
        final_content="💬 评论: 已写回",
    )
    assert gate.ok is True


def test_extract_ticket_evidence_includes_bags():
    from ticket_completion_gate import extract_ticket_evidence_from_messages

    messages = [
        _tool(
            "find_bags_near_time",
            {"matched": [{"file_name": "all_x.bag"}]},
        ),
        _tool(
            "create_ticket_comment",
            {"success": True, "verified": True, "ticketId": "215372"},
        ),
    ]
    ev = extract_ticket_evidence_from_messages(messages, "215372")
    assert "bags" in ev
    assert "all_x.bag" in ev["bags"]["located"]
    assert ev["bags"]["attached"] is False
