"""工单运行时约束：调工具前拦截 + 收工前评论/结案门禁。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

# 同一 run 最多注入几次门禁重试
MAX_TICKET_GATE_INJECTS = 3

_CLOSING_STATUSES = frozenset({3, 5, 6})

# 评论/汇报里「未修复、需人工」信号（通用，不绑具体故障码）：配 status=3 视为违规结案
_UNRESOLVED_SUGGESTION_MARKERS = (
    "请现场",
    "建议现场",
    "建议：请",
    "请人工",
    "建议人工",
    "待人工",
    "需人工",
    "需要人工",
    "请工程师",
    "无法远程",
    "当前无法远程",
    "无法远程修复",
    "已转问题分析",
    "现场确认",
    "现场处理",
    "现场跟进",
)

# 与「仅建议」并存时，须另有恢复证据才允许 3
_RECOVERY_EVIDENCE_MARKERS = (
    "已恢复",
    "已释放",
    "已清除",
    "影子当前无活跃",
    "无需远程处置，已完成",
    "现象已消除",
    "同码已不成立",
)

_TICKET_ID_PATTERNS = (
    re.compile(r"ticket_id\s*[=：:]\s*(\d+)", re.IGNORECASE),
    re.compile(r"ticketId\s*[=：:]\s*(\d+)", re.IGNORECASE),
    re.compile(r"【BMS工单AI处理】[^\d]*(\d{5,})"),
    re.compile(r"\bid\s*[=：:]\s*(\d{5,})\b", re.IGNORECASE),
    re.compile(r"\(id\s*=\s*(\d+)\)", re.IGNORECASE),
)

_PROGRESS_PHRASES = (
    "正在处理中",
    "等待终端",
    "命令已发送",
    "正在接收",
    "等待终端响应",
)

# 工单任务却按非工单收工的口误/错模板
_NON_TICKET_CLAIM_MARKERS = (
    "非工单场景",
    "非工单模式",
    "属于非工单",
    "按非工单",
    "走非工单",
    "仅采集模式",
    "仅采集场景",
    "无 ticket_id",
    "无【BMS工单AI处理】",
    "消息中无 ticket",
    "不属于工单",
)

_HONEST_FAILURE_MARKERS = (
    "评论失败",
    "未写回",
    "评论: 失败",
    "评论：失败",
    "💬 评论: 失败",
    "💬 评论：失败",
)

_CLAIM_WRITTEN_MARKERS = (
    "评论: 已写回",
    "评论：已写回",
    "💬 评论: 已写回",
    "💬 评论：已写回",
)


@dataclass(frozen=True)
class TicketGateResult:
    """门禁判定结果。"""

    applicable: bool
    """任务是否含 ticket_id（需门禁）。"""
    ticket_id: Optional[str]
    ok: bool
    """是否允许以当前正文收工。"""
    reason: str
    """未通过时的原因码 / 通过说明。"""
    retry_message: str
    """注入给模型的重试提示（未通过时）。"""


def extract_ticket_id_from_text(text: str) -> Optional[str]:
    """从任务或正文中解析工单数字 ID。"""
    if not text:
        return None
    for pat in _TICKET_ID_PATTERNS:
        m = pat.search(text)
        if m:
            return str(m.group(1)).strip()
    return None


def extract_ticket_evidence_from_messages(
    messages: list[Any], ticket_id: str | None = None
) -> dict[str, Any]:
    """从子代理会话提取可供父会话门禁使用的写评/改状态/录包附件证据。"""
    evidence: dict[str, Any] = {}
    tid = str(ticket_id).strip() if ticket_id else None
    last_comment: dict[str, Any] | None = None
    last_status: dict[str, Any] | None = None
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        name = (msg.get("name") or "").strip()
        data = _parse_json_content(msg.get("content"))
        if not isinstance(data, dict):
            continue
        result_tid = data.get("ticketId") or data.get("ticket_id")
        if tid and result_tid is not None and str(result_tid).strip() != tid:
            continue
        if name == "create_ticket_comment":
            if (
                _truthy(data.get("verified"))
                or _truthy(data.get("reused_existing"))
                or _truthy(data.get("blocked_duplicate"))
            ):
                last_comment = data
        elif name == "change_ticket_status" and _truthy(data.get("success")):
            try:
                status_int = int(data.get("status"))
            except (TypeError, ValueError):
                continue
            if status_int in _CLOSING_STATUSES:
                last_status = data
    if last_comment is not None:
        evidence["create_ticket_comment"] = last_comment
    if last_status is not None:
        evidence["change_ticket_status"] = last_status
    located = collect_located_bag_names(messages)
    if located:
        check_tid = tid or ""
        evidence["bags"] = {
            "located": located,
            "attached": bool(check_tid and has_bag_ticket_attachment(messages, check_tid)),
        }
    return evidence


def extract_ticket_id_from_messages(messages: list[Any], task: str = "") -> Optional[str]:
    """优先任务文案，其次 get_ticket 工具回执中的 id。"""
    tid = extract_ticket_id_from_text(task or "")
    if tid:
        return tid
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "tool":
            continue
        name = (msg.get("name") or "").strip()
        if name != "get_ticket":
            continue
        data = _parse_json_content(msg.get("content"))
        if not isinstance(data, dict):
            continue
        raw = data.get("id") or data.get("ticketId") or data.get("ticket_id")
        if raw is not None and str(raw).strip().isdigit():
            return str(raw).strip()
    return extract_ticket_id_from_text(
        " ".join(
            str(m.get("content") or "")
            for m in (messages or [])
            if isinstance(m, dict) and m.get("role") == "user"
        )
    )


def _parse_json_content(content: Any) -> Any:
    if content is None:
        return None
    if isinstance(content, dict):
        return content
    text = str(content).strip()
    if not text:
        return None
    if not text.startswith("{"):
        idx = text.find("{")
        if idx >= 0:
            text = text[idx:]
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def iter_create_comment_results(messages: list[Any]) -> list[dict[str, Any]]:
    """收集本会话 create_ticket_comment 工具回执（含 subagent 回传的 ticket_evidence）。"""
    out: list[dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "tool":
            continue
        name = (msg.get("name") or "").strip()
        data = _parse_json_content(msg.get("content"))
        if not isinstance(data, dict):
            continue
        if name == "create_ticket_comment":
            out.append(data)
            continue
        if name in ("subagent", "spawn_subagent", "run_subagent"):
            evidence = data.get("ticket_evidence")
            if isinstance(evidence, dict):
                comment = evidence.get("create_ticket_comment")
                if isinstance(comment, dict):
                    out.append(comment)
    return out


def iter_status_change_results(messages: list[Any]) -> list[dict[str, Any]]:
    """收集 change_ticket_status 回执（含 subagent ticket_evidence）。"""
    out: list[dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        name = (msg.get("name") or "").strip()
        data = _parse_json_content(msg.get("content"))
        if not isinstance(data, dict):
            continue
        if name == "change_ticket_status":
            out.append(data)
            continue
        if name in ("subagent", "spawn_subagent", "run_subagent"):
            evidence = data.get("ticket_evidence")
            if isinstance(evidence, dict):
                status = evidence.get("change_ticket_status")
                if isinstance(status, dict):
                    out.append(status)
    return out


def has_verified_comment_for_ticket(messages: list[Any], ticket_id: str) -> bool:
    """是否存在对本 ticket_id 的 verified=true 写评回执（含沿用已有评论 / 子代理回传）。"""
    tid = str(ticket_id).strip()
    for data in iter_create_comment_results(messages):
        result_tid = data.get("ticketId") or data.get("ticket_id")
        if result_tid is None or str(result_tid).strip() != tid:
            continue
        if _truthy(data.get("verified")):
            return True
        # MCP 防双发：已有评论未再写入，仍视为本单评论已落库
        if _truthy(data.get("reused_existing")) or _truthy(data.get("blocked_duplicate")):
            if data.get("existingComment") or data.get("matchedComment"):
                return True
    return False


def has_failed_comment_attempt_for_ticket(
    messages: list[Any], ticket_id: str
) -> bool:
    """是否已对本 ticket_id 调用写评且 verified 未通过（含工具不存在等非 JSON 失败）。"""
    tid = str(ticket_id).strip()
    for data in iter_create_comment_results(messages):
        result_tid = data.get("ticketId") or data.get("ticket_id")
        if result_tid is None or str(result_tid).strip() != tid:
            continue
        if not _truthy(data.get("verified")):
            return True
    if has_comment_tool_unavailable(messages):
        # 父代理无 MCP：调用过写评但工具不存在，视为失败尝试
        return True
    return False


def has_comment_tool_unavailable(messages: list[Any]) -> bool:
    """会话是否出现过 create_ticket_comment 工具不可用（父代理无 ticket_ops MCP）。"""
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        name = (msg.get("name") or "").strip()
        if name != "create_ticket_comment":
            continue
        content = str(msg.get("content") or "")
        low = content.lower()
        if "不存在" in content or "not found" in low or "unknown tool" in low:
            return True
        data = _parse_json_content(content)
        if isinstance(data, dict):
            err = str(data.get("error") or "")
            if "不存在" in err or "not found" in err.lower():
                return True
    return False


def is_numeric_ticket_id(ticket_id: str) -> bool:
    return bool(re.fullmatch(r"\d{3,}", str(ticket_id or "").strip()))

def count_verified_comments_for_ticket(messages: list[Any], ticket_id: str) -> int:
    """本会话对本 ticket_id 已核实写评次数。"""
    tid = str(ticket_id).strip()
    n = 0
    for data in iter_create_comment_results(messages):
        if not _truthy(data.get("verified")):
            continue
        result_tid = data.get("ticketId") or data.get("ticket_id")
        if result_tid is not None and str(result_tid).strip() == tid:
            n += 1
    return n


def has_closing_status_after_verified_comment(
    messages: list[Any], ticket_id: str
) -> bool:
    """写评 verified 之后，是否有成功的结案改状态（3/5/6）。

    接手时 1/2→5 不算：必须出现在本单 verified 写评回执之后。
    """
    return get_closing_status_after_verified_comment(messages, ticket_id) is not None


def get_closing_status_after_verified_comment(
    messages: list[Any], ticket_id: str
) -> Optional[int]:
    """写评 verified 之后最近一次成功结案状态（3/5/6），无则 None。

    也接受 subagent.ticket_evidence 中带回的改状态（父子会话不同步时）。
    """
    tid = str(ticket_id).strip()
    if not has_verified_comment_for_ticket(messages, tid):
        return None

    # 优先：同会话内「写评之后」的改状态
    seen_verified = False
    last_status: Optional[int] = None
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        name = (msg.get("name") or "").strip()
        data = _parse_json_content(msg.get("content"))
        if not isinstance(data, dict):
            continue
        if name == "create_ticket_comment":
            result_tid = data.get("ticketId") or data.get("ticket_id")
            if result_tid is not None and str(result_tid).strip() == tid:
                if (
                    _truthy(data.get("verified"))
                    or _truthy(data.get("reused_existing"))
                    or _truthy(data.get("blocked_duplicate"))
                ):
                    seen_verified = True
            continue
        if name in ("subagent", "spawn_subagent", "run_subagent"):
            evidence = data.get("ticket_evidence")
            if isinstance(evidence, dict):
                c = evidence.get("create_ticket_comment")
                if isinstance(c, dict):
                    ct = c.get("ticketId") or c.get("ticket_id")
                    if ct is not None and str(ct).strip() == tid:
                        if (
                            _truthy(c.get("verified"))
                            or _truthy(c.get("reused_existing"))
                            or _truthy(c.get("blocked_duplicate"))
                        ):
                            seen_verified = True
                s = evidence.get("change_ticket_status")
                if isinstance(s, dict) and seen_verified:
                    stid = s.get("ticketId") or s.get("ticket_id")
                    if stid is not None and str(stid).strip() == tid and _truthy(
                        s.get("success")
                    ):
                        try:
                            status_int = int(s.get("status"))
                        except (TypeError, ValueError):
                            continue
                        if status_int in _CLOSING_STATUSES:
                            last_status = status_int
            continue
        if not seen_verified or name != "change_ticket_status":
            continue
        result_tid = data.get("ticketId") or data.get("ticket_id")
        if result_tid is None or str(result_tid).strip() != tid:
            continue
        if not _truthy(data.get("success")):
            continue
        try:
            status_int = int(data.get("status"))
        except (TypeError, ValueError):
            continue
        if status_int in _CLOSING_STATUSES:
            last_status = status_int
    if last_status is not None:
        return last_status
    return None


def extract_verified_comment_texts(messages: list[Any], ticket_id: str) -> list[str]:
    """从助手 tool_call 参数中提取对本单已核实写评的 content。"""
    tid = str(ticket_id).strip()
    verified_ids: set[str] = set()
    for data in iter_create_comment_results(messages):
        if not _truthy(data.get("verified")):
            continue
        result_tid = data.get("ticketId") or data.get("ticket_id")
        if result_tid is not None and str(result_tid).strip() == tid:
            verified_ids.add(tid)
    if not verified_ids:
        return []

    texts: list[str] = []
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function") or {}
            if (func.get("name") or "").strip() != "create_ticket_comment":
                continue
            raw_args = func.get("arguments") or {}
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    raw_args = {}
            if not isinstance(raw_args, dict):
                continue
            arg_tid = str(
                raw_args.get("ticket_id") or raw_args.get("ticketId") or ""
            ).strip()
            if arg_tid and arg_tid != tid:
                continue
            content = str(raw_args.get("content") or "").strip()
            if content:
                texts.append(content)
    return texts


def is_unresolved_suggestion_text(text: str) -> bool:
    """评论/汇报是否像「仅诊断+建议人工、未证明已恢复」。"""
    body = text or ""
    if not any(m in body for m in _UNRESOLVED_SUGGESTION_MARKERS):
        return False
    if any(m in body for m in _RECOVERY_EVIDENCE_MARKERS):
        return False
    return True


def status3_blocked_by_unresolved_suggestion(
    messages: list[Any], ticket_id: str, final_content: str = ""
) -> bool:
    """改 3 但评论/汇报仍是「请现场/无法远程」且无恢复证据 → 应拦截。"""
    status = get_closing_status_after_verified_comment(messages, ticket_id)
    if status != 3:
        return False
    for text in extract_verified_comment_texts(messages, ticket_id):
        if is_unresolved_suggestion_text(text):
            return True
    if is_unresolved_suggestion_text(final_content or ""):
        return True
    return False


def _truthy(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)) and value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes"):
        return True
    return False


def is_progress_only_reply(content: str) -> bool:
    """最终正文是否像进度话术（应拒绝收工）。"""
    text = (content or "").strip()
    if not text:
        return False
    for phrase in _PROGRESS_PHRASES:
        if phrase in text:
            return True
    return False


def claims_non_ticket_scenario(content: str) -> bool:
    """工单任务正文是否口误成非工单/仅采集。"""
    text = content or ""
    return any(m in text for m in _NON_TICKET_CLAIM_MARKERS)


def uses_non_ticket_report_format(content: str) -> bool:
    """是否用了非工单「📋 设备」模板且没有「📋 工单」。"""
    text = content or ""
    has_device = ("📋 设备:" in text) or ("📋 设备：" in text)
    has_ticket = ("📋 工单:" in text) or ("📋 工单：" in text)
    return has_device and not has_ticket


def has_successful_tool_call(messages: list[Any], tool_name: str) -> bool:
    """本会话是否有指定工具的成功回执（含 subagent ticket_evidence 透传）。"""
    want = (tool_name or "").strip()
    if not want:
        return False
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        name = (msg.get("name") or "").strip()
        data = _parse_json_content(msg.get("content"))
        if not isinstance(data, dict):
            continue
        if name == want:
            if data.get("success") is False:
                continue
            if (
                _truthy(data.get("success"))
                or data.get("returnCode") in (200, "200")
                or _truthy(data.get("ok"))
            ):
                return True
            # 部分 MCP 成功时只有 data 字段
            if data.get("error") in (None, "") and (
                data.get("data") is not None or data.get("mode") is not None
            ):
                return True
            continue
        if name in ("subagent", "spawn_subagent", "run_subagent"):
            evidence = data.get("ticket_evidence")
            if isinstance(evidence, dict):
                nested = evidence.get(want)
                if isinstance(nested, dict) and data.get("success") is not False:
                    if (
                        _truthy(nested.get("success"))
                        or nested.get("returnCode") in (200, "200")
                    ):
                        return True
    return False


# 声称「已远程执行某动作」时，必须有对应成功工具回执（防编造）
_REMOTE_ACTION_CLAIM_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "已远程下发指令将",
            "已远程将 control_mode",
            "已远程切换 control_mode",
            "已切换为 AUTO",
            "切换为 `AUTO`",
            "切换为 AUTO",
            "control_mode 切换为",
            "control_mode` 切换为",
            "已远程下发.*自动模式",
            "set_control_mode",
        ),
        "set_control_mode",
    ),
    (
        ("已远程下发回桩", "已远程回桩", "已下发回桩"),
        "device_back_to_station",
    ),
    (
        ("已远程倒退", "已下发倒退", "已远程脱困"),
        "device_backward",
    ),
    (
        ("已软重启", "已远程重启", "已下发软重启"),
        "soft_restart",
    ),
    (
        ("已重定位", "已远程重定位", "已下发重定位"),
        "relocate",
    ),
)


def _text_claims_remote_action(text: str, markers: tuple[str, ...]) -> bool:
    body = text or ""
    for m in markers:
        if ".*" in m:
            if re.search(m, body, re.IGNORECASE):
                return True
        elif m in body:
            return True
    return False


def invented_remote_action_gap(
    messages: list[Any], *texts: str
) -> Optional[str]:
    """正文/评论声称已远程处置，但会话无对应成功工具回执。"""
    blobs = [t for t in texts if t]
    if not blobs:
        return None
    for markers, tool in _REMOTE_ACTION_CLAIM_RULES:
        if not any(_text_claims_remote_action(t, markers) for t in blobs):
            continue
        if has_successful_tool_call(messages, tool):
            continue
        return (
            f"声称已远程执行与「{tool}」相关的动作，但本会话无该工具的成功回执；"
            f"禁止编造已下发/已切换。请删除虚构叙述，或先实际调用 {tool}（须当前剧本允许）后再写评。"
        )
    return None


def claims_comment_written(content: str) -> bool:
    text = content or ""
    return any(m in text for m in _CLAIM_WRITTEN_MARKERS)


def admits_comment_failure(content: str) -> bool:
    text = content or ""
    return any(m in text for m in _HONEST_FAILURE_MARKERS)


def build_retry_message(ticket_id: str, reason: str) -> str:
    return (
        f"【工单评论硬门禁】任务 ticket_id={ticket_id} 尚未允许收工：{reason}\n"
        f"必须：create_ticket_comment(ticket_id=\"{ticket_id}\", content=…) "
        f"且回执 verified=true；随后 change_ticket_status 到 3/5/6（写评之后）。\n"
        f"仅诊断+建议现场处理 → 必须改 status=6，禁止改 3。"
        f"改 3 仅当本单现象已消除且有影子/日志恢复证据。\n"
        f"禁止写到其它工单；禁止重复写评；禁止以「正在处理中 / 等待终端 / 💬 评论: 已写回」空喊收工。\n"
        f"缺评时优先写评结案；已找到日志文件须先完成时间窗抽取，"
        f"禁止用 tail/影子空转代替写评。\n"
        f"若你是零号员工/父代理且 create_ticket_comment 返回「工具不存在」："
        f"禁止再自调写评/钉钉/飞书；必须 subagent(template=设备运维) 让子代理写评+改状态。\n"
        f"ticket_id 必须用数字 id（如 215367），禁止用工单编号 SH20… 当 ticket_id。\n"
        f"若写评确实失败：汇报须写明「评论失败/未写回」，不要写已写回。"
    )


def build_parent_redispatch_message(ticket_id: str, reason: str) -> str:
    """父代理无工单 MCP 时的门禁重试：只允许重派设备运维。"""
    return (
        f"【工单评论硬门禁】ticket_id={ticket_id}：{reason}\n"
        f"你当前会话没有 create_ticket_comment / ticket_ops。"
        f"禁止再调用 create_ticket_comment、ticket_ops、钉钉、飞书冒充写回。\n"
        f"唯一动作：subagent(template=设备运维)，任务写明 "
        f"ticket_id={ticket_id}（数字 id）须 create_ticket_comment verified + "
        f"change_ticket_status(3/5/6) 后按 📋 工单 格式回报。\n"
        f"若子代理已诚实失败（评论失败/BMS 500），对用户汇报「评论失败/未写回」即可收工。"
    )


# ── 工单终端命令形态（防空转；不做总条数硬封顶，避免 ls 占满后无法时间窗抽取）──

MAX_SAME_COMMAND = 2
MAX_SHADOW_CALLS = 3

_TERMINAL_ENTRY_TOOLS = frozenset(
    {"send_command", "interactive_session", "execute_with_retry"}
)

_TAIL_OR_CAT_RE = re.compile(r"(^|[|&;]\s*)(tail|cat)\b", re.IGNORECASE)
_HAS_HEAD_RE = re.compile(r"\bhead\b", re.IGNORECASE)
_HAS_GREP_RE = re.compile(r"\bgrep\b", re.IGNORECASE)
_HAS_LS_RE = re.compile(r"\bls\b", re.IGNORECASE)


def normalize_terminal_command(command: str) -> str:
    """规范化命令以便同命令去重计数。"""
    text = (command or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def is_forbidden_tail_or_cat(command: str) -> bool:
    """禁止用 tail/cat 读文件末尾/全文冒充 happenTime 证据。"""
    return bool(_TAIL_OR_CAT_RE.search(command or ""))


def is_unbounded_log_grep(command: str) -> bool:
    """对日志文件的 grep 必须带 head 封顶；ls|grep 文件名探测除外。"""
    cmd = command or ""
    if not _HAS_GREP_RE.search(cmd):
        return False
    if _HAS_HEAD_RE.search(cmd):
        return False
    # ls … | grep 日期 → 列文件名，允许
    first = cmd.split("|", 1)[0]
    if _HAS_LS_RE.search(first) and not _HAS_GREP_RE.search(first):
        return False
    # 像在读路径/按日日志文件
    if "/" in cmd or re.search(r"_\d{4}-\d{2}-\d{2}\b", cmd):
        return True
    return False


def terminal_command_shape_error(command: str) -> Optional[str]:
    """命令形态违规时返回文案。"""
    cmd = command or ""
    if is_forbidden_tail_or_cat(cmd):
        return (
            "禁止用文件末尾（tail/cat）冒充 happenTime 证据；"
            "请时间串 grep … | head -n 20，或交编排写负结果评论并 change_ticket_status(6)。"
        )
    if is_unbounded_log_grep(cmd):
        return (
            "大日志 grep 必须加 | head -n 20（或等价封顶）；"
            "禁止无 head 的全日/全文件 grep。"
        )
    return None


def _parse_tool_call_args(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            data = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def iter_assistant_tool_calls(
    messages: list[Any],
) -> list[tuple[str, dict[str, Any]]]:
    """按出现顺序收集助手侧 tool_calls (name, args)。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function") or {}
            name = (func.get("name") or "").strip()
            if not name:
                continue
            out.append((name, _parse_tool_call_args(func.get("arguments"))))
    return out


def iter_past_terminal_commands(messages: list[Any]) -> list[str]:
    """会话内已发起的终端命令条目（send_command / execute_with_retry + interactive 内条目）。"""
    cmds: list[str] = []
    for name, args in iter_assistant_tool_calls(messages):
        if name in ("send_command", "execute_with_retry"):
            cmd = str(args.get("command") or "").strip()
            if cmd:
                cmds.append(cmd)
        elif name == "interactive_session":
            for item in args.get("commands") or []:
                cmd = str(item or "").strip()
                if cmd:
                    cmds.append(cmd)
    return cmds


def count_terminal_entries(messages: list[Any]) -> int:
    """会话内终端命令条数（仅统计/诊断用，不再作硬封顶）。"""
    return len(iter_past_terminal_commands(messages))


def count_same_command(messages: list[Any], command: str) -> int:
    target = normalize_terminal_command(command)
    if not target:
        return 0
    return sum(
        1
        for c in iter_past_terminal_commands(messages)
        if normalize_terminal_command(c) == target
    )


def count_shadow_calls(messages: list[Any]) -> int:
    return sum(
        1 for name, _ in iter_assistant_tool_calls(messages) if name == "get_device_shadow"
    )


_BAG_ATTACH_FAIL_MARKERS = (
    "录包上传未返回",
    "未返回 url",
    "未解析到 url",
    "未挂录包",
    "录包未挂",
    "请人工挂载",
    "请人工下载挂附件",
    "需人工挂载录包",
    "attach_ready=false",
    "无 url 无法挂",
)


def admits_bag_attach_failure(text: str) -> bool:
    body = text or ""
    return any(m in body for m in _BAG_ATTACH_FAIL_MARKERS)


def collect_located_bag_names(messages: list[Any]) -> list[str]:
    """本会话已定位/已尝试上传的录包文件名。"""
    names: list[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        if not isinstance(raw, str):
            return
        name = raw.strip().replace("\\", "/").split("/")[-1]
        if not name or name in seen:
            return
        low = name.lower()
        if (
            ".bag" in low
            or low.endswith(".active")
            or low.startswith("all_")
            or low.startswith("task_")
            or name == "matched_bag"
        ):
            seen.add(name)
            names.append(name)

    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        tool = (msg.get("name") or "").strip()
        data = _parse_json_content(msg.get("content"))
        if not isinstance(data, dict):
            continue
        if tool in ("subagent", "spawn_subagent", "run_subagent"):
            bags = data.get("ticket_evidence", {})
            if isinstance(bags, dict):
                bags = bags.get("bags")
            if isinstance(bags, dict):
                for item in bags.get("located") or []:
                    _add(item)
            continue
        if tool == "find_bags_near_time":
            for item in data.get("matched") or []:
                if isinstance(item, dict):
                    _add(item.get("file_name") or item.get("file_path"))
            try:
                matched_n = int(data.get("matched_count") or 0)
            except (TypeError, ValueError):
                matched_n = 0
            if matched_n > 0 and not names:
                _add("matched_bag")
        elif tool in ("upload_bag_file", "upload_bag_list"):
            _add(data.get("bag_name"))
            d = data.get("data")
            if isinstance(d, dict):
                _add(d.get("bag_name") or d.get("file_name"))
            _add(str(data.get("file_path") or data.get("filePath") or ""))
    return names


def has_bag_ticket_attachment(messages: list[Any], ticket_id: str) -> bool:
    """是否已对本单成功挂载录包附件（name/url 含 .bag）。"""
    tid = str(ticket_id).strip()
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        tool = (msg.get("name") or "").strip()
        data = _parse_json_content(msg.get("content"))
        if not isinstance(data, dict):
            continue
        if tool in ("subagent", "spawn_subagent", "run_subagent"):
            bags = data.get("ticket_evidence", {})
            if isinstance(bags, dict):
                bags = bags.get("bags")
            if isinstance(bags, dict) and _truthy(bags.get("attached")):
                return True
            continue
        if tool != "create_ticket_attachment":
            continue
        result_tid = data.get("ticketId") or data.get("ticket_id")
        if result_tid is not None and str(result_tid).strip() != tid:
            continue
        if data.get("success") is False:
            continue
        blob = f"{data.get('name') or ''} {data.get('url') or ''}".lower()
        if ".bag" in blob:
            return True
    for name, args in iter_assistant_tool_calls(messages):
        if name != "create_ticket_attachment":
            continue
        arg_tid = str(args.get("ticket_id") or args.get("ticketId") or "").strip()
        if arg_tid and arg_tid != tid:
            continue
        blob = f"{args.get('name') or ''} {args.get('url') or ''}".lower()
        if ".bag" in blob:
            return True
    return False


def bag_attachment_gap(
    messages: list[Any], ticket_id: str, final_content: str = ""
) -> Optional[str]:
    """定位到录包却未挂附件、且评论未诚实说明 → 返回拦截原因。"""
    located = collect_located_bag_names(messages)
    if not located:
        return None
    if has_bag_ticket_attachment(messages, ticket_id):
        return None
    texts = list(extract_verified_comment_texts(messages, ticket_id))
    texts.append(final_content or "")
    joined = "\n".join(texts)
    if admits_bag_attach_failure(joined):
        return None
    real = [n for n in located if n != "matched_bag"] or located
    sample = ", ".join(real[:3])
    return (
        f"已定位录包（{sample}）但未 create_ticket_attachment 挂 .bag；"
        f"有 url 必须挂附件；无 url 须在评论写明「录包上传未返回 url，请人工挂载 …」"
        f"（禁止只挂相机却把录包写成已挂载）"
    )


def build_bag_attach_retry_message(ticket_id: str, reason: str) -> str:
    return (
        f"【工单录包附件硬门禁】ticket_id={ticket_id}：{reason}\n"
        f"立刻补做：upload_bag_file 若返回 url → create_ticket_attachment(name=xxx.bag, url=…)；\n"
        f"若 url 为空/attach_ready=false → 改写评论说明未挂录包原因，禁止伪称附件已含录包。\n"
        f"然后再确认 change_ticket_status(3/5/6)。"
    )


def evaluate_ticket_completion_gate(
    *,
    task: str,
    messages: list[Any],
    final_content: str,
) -> TicketGateResult:
    """判定当前无工具最终回复是否允许工单任务收工。"""
    ticket_id = extract_ticket_id_from_messages(messages, task=task)
    if not ticket_id:
        return TicketGateResult(
            applicable=False,
            ticket_id=None,
            ok=True,
            reason="no_ticket_id",
            retry_message="",
        )

    if claims_non_ticket_scenario(final_content) or uses_non_ticket_report_format(
        final_content
    ):
        reason = (
            "本任务含 ticket_id/【BMS工单AI处理】，禁止按「非工单/仅采集」收工或使用 "
            "「📋 设备」模板；须走 ticket-handling：取证后 create_ticket_comment → "
            "change_ticket_status(3/5/6)，汇报用「📋 工单」格式"
        )
        return TicketGateResult(
            applicable=True,
            ticket_id=ticket_id,
            ok=False,
            reason=reason,
            retry_message=build_retry_message(ticket_id, reason),
        )

    comment_texts = extract_verified_comment_texts(messages, ticket_id)
    invent_gap = invented_remote_action_gap(
        messages, final_content, *comment_texts
    )
    if invent_gap:
        return TicketGateResult(
            applicable=True,
            ticket_id=ticket_id,
            ok=False,
            reason=invent_gap,
            retry_message=build_retry_message(ticket_id, invent_gap),
        )

    bag_gap = bag_attachment_gap(messages, ticket_id, final_content)
    if bag_gap:
        return TicketGateResult(
            applicable=True,
            ticket_id=ticket_id,
            ok=False,
            reason=bag_gap,
            retry_message=build_bag_attach_retry_message(ticket_id, bag_gap),
        )

    if is_progress_only_reply(final_content):
        reason = "最终回复含进度话术（正在处理中/等待终端等），未完成取证结案"
        return TicketGateResult(
            applicable=True,
            ticket_id=ticket_id,
            ok=False,
            reason=reason,
            retry_message=build_retry_message(ticket_id, reason),
        )

    if has_verified_comment_for_ticket(messages, ticket_id):
        if status3_blocked_by_unresolved_suggestion(
            messages, ticket_id, final_content
        ):
            reason = (
                f"评论/汇报仍为「请现场/请人工/无法远程」等未修复升级，却改 status=3；"
                f"仅诊断≠闭环（适用一切故障），请改 change_ticket_status(..., 6)"
            )
            return TicketGateResult(
                applicable=True,
                ticket_id=ticket_id,
                ok=False,
                reason=reason,
                retry_message=build_retry_message(ticket_id, reason),
            )
        if has_closing_status_after_verified_comment(messages, ticket_id):
            return TicketGateResult(
                applicable=True,
                ticket_id=ticket_id,
                ok=True,
                reason="verified_comment_and_status",
                retry_message="",
            )
        reason = (
            f"已有 verified 写评，但写评之后缺少对本单 ticket_id={ticket_id} 的 "
            f"change_ticket_status(3/5/6)"
        )
        return TicketGateResult(
            applicable=True,
            ticket_id=ticket_id,
            ok=False,
            reason=reason,
            retry_message=build_retry_message(ticket_id, reason),
        )

    if has_failed_comment_attempt_for_ticket(messages, ticket_id) and admits_comment_failure(
        final_content
    ):
        return TicketGateResult(
            applicable=True,
            ticket_id=ticket_id,
            ok=True,
            reason="honest_failure",
            retry_message="",
        )

    # 父代理无 MCP：写评工具不存在 → 不要逼它自调，改为重派或诚实失败收工
    if has_comment_tool_unavailable(messages):
        if admits_comment_failure(final_content):
            return TicketGateResult(
                applicable=True,
                ticket_id=ticket_id,
                ok=True,
                reason="honest_failure_tool_unavailable",
                retry_message="",
            )
        reason = (
            f"create_ticket_comment 在本会话不可用（工具不存在）；"
            f"须委派设备运维写评，或汇报「评论失败/未写回」"
        )
        return TicketGateResult(
            applicable=True,
            ticket_id=ticket_id,
            ok=False,
            reason=reason,
            retry_message=build_parent_redispatch_message(ticket_id, reason),
        )

    if claims_comment_written(final_content):
        reason = (
            f"正文声称「评论已写回」，但本会话无 ticketId={ticket_id} 且 verified=true 的 "
            f"create_ticket_comment 回执（禁止编造/写错单）"
        )
    else:
        reason = (
            f"工单任务未完结：缺少对本单 ticket_id={ticket_id} 的 "
            f"create_ticket_comment（verified=true）"
        )

    return TicketGateResult(
        applicable=True,
        ticket_id=ticket_id,
        ok=False,
        reason=reason,
        retry_message=build_retry_message(ticket_id, reason),
    )


def subagent_comment_claim_unverified(
    *,
    task: str,
    result_text: str,
    messages: list[Any] | None = None,
) -> bool:
    """父代理侧：子结果声称已写回，但会话无对本单 verified 写评。"""
    if not claims_comment_written(result_text or ""):
        return False
    ticket_id = extract_ticket_id_from_messages(messages or [], task=task)
    if not ticket_id:
        ticket_id = extract_ticket_id_from_text(task or "")
    if not ticket_id:
        return True
    if not has_verified_comment_for_ticket(messages or [], ticket_id):
        return True
    # 已核实写评但仍缺写评后结案改状态 → 也视为未完成
    return not has_closing_status_after_verified_comment(messages or [], ticket_id)


# ── 调工具前拦截（原 ticket_tool_guard）──────────────────────────────

# 与 playbook-call-sequences.json 对齐：至少 locate / evidence 的 forbidden_tools
_SKILL_FORBIDDEN_TOOLS: dict[str, frozenset[str]] = {
    "device-evidence-collect": frozenset(
        {
            "soft_restart",
            "device_backward",
            "relocate",
            "set_control_mode",
            "factory_reset",
            "remote_action",
            "device_back_to_station",
        }
    ),
    "device-remote-operations": frozenset(
        {
            "soft_restart",
            "device_backward",
            "set_control_mode",
            "factory_reset",
            "remote_action",
        }
    ),
}

_DINGTALK_EXTRA = frozenset({"send_image_to_dingtalk"})

_TICKET_MODE_MARKERS = (
    "【BMS工单AI处理】",
    "BMS工单",
    "ticket_id",
    "ticketId",
)


def is_ticket_mode(task: str = "", messages: list[Any] | None = None) -> bool:
    """任务或会话是否处于工单处理模式。"""
    text = task or ""
    if extract_ticket_id_from_text(text):
        return True
    if any(m in text for m in _TICKET_MODE_MARKERS):
        return True
    if messages and extract_ticket_id_from_messages(messages, task=text):
        return True
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = str(msg.get("content") or "")
        if extract_ticket_id_from_text(content):
            return True
        if any(m in content for m in _TICKET_MODE_MARKERS):
            return True
    return False


def _active_skill_names(active_skills: Iterable[str] | None) -> set[str]:
    return {str(s).strip() for s in (active_skills or []) if str(s).strip()}


def deny_tool_in_ticket_mode(
    *,
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
    task: str = "",
    messages: list[Any] | None = None,
    active_skills: Iterable[str] | None = None,
) -> Optional[str]:
    """若应拦截则返回错误文案，否则 None。"""
    if not is_ticket_mode(task, messages):
        return None

    name = (tool_name or "").strip()
    if not name:
        return None

    msgs = messages or []
    args = tool_args or {}
    expected_tid = extract_ticket_id_from_messages(msgs, task=task)

    if name.startswith("dingtalk_") or name in _DINGTALK_EXTRA:
        return (
            "工单模式禁止调用钉钉工具（含工作通知/群消息/Webhook）。"
            "评论失败请重试工单写回或对内汇报失败原因，禁止降级钉钉。"
        )

    skills = _active_skill_names(active_skills)
    for skill, forbidden in _SKILL_FORBIDDEN_TOOLS.items():
        if skill in skills and name in forbidden:
            return (
                f"当前已加载技能「{skill}」，禁止调用控制类工具「{name}」。"
                f"请按该技能允许的取证/定位步骤执行，或转人工结案。"
            )

    # 影子软限：防当前态轮询代替 happenTime 结案
    if name == "get_device_shadow":
        past = count_shadow_calls(msgs)
        if past >= MAX_SHADOW_CALLS:
            return (
                f"本单 get_device_shadow 已达上限 {MAX_SHADOW_CALLS} 次；"
                f"当前影子≠故障时刻证据。请写评结案（负结果 → status=6），"
                f"禁止再查影子空转。"
            )

    # 终端：禁 tail/cat、大日志 grep 须 head、同命令去重（不做总条数硬封顶）
    if name in _TERMINAL_ENTRY_TOOLS:
        proposed: list[str] = []
        if name in ("send_command", "execute_with_retry"):
            cmd = str(args.get("command") or "").strip()
            if cmd:
                proposed = [cmd]
        else:
            proposed = [
                str(c or "").strip()
                for c in (args.get("commands") or [])
                if str(c or "").strip()
            ]

        for cmd in proposed:
            shape_err = terminal_command_shape_error(cmd)
            if shape_err:
                return shape_err
            same = count_same_command(msgs, cmd)
            if same >= MAX_SAME_COMMAND:
                return (
                    f"同一终端命令已执行 {same} 次（上限 {MAX_SAME_COMMAND}），禁止再发。"
                    f"已找到文件则立刻时间窗 grep|head 抽取；空命中则换候选或写评结案。"
                )

    if name == "create_ticket_comment":
        req_tid = str(
            args.get("ticket_id") or args.get("ticketId") or ""
        ).strip()
        if req_tid and not is_numeric_ticket_id(req_tid):
            return (
                f"ticket_id 必须用数字 id，禁止用工单编号「{req_tid}」。"
                f"请先 get_ticket 取 id（如 215367），再 create_ticket_comment。"
            )
        expected = expected_tid
        if expected and req_tid and req_tid != expected:
            return (
                f"禁止写到其它工单：本单 ticket_id={expected}，"
                f"请求 ticket_id={req_tid}"
            )
        check_tid = expected or req_tid
        if check_tid and has_verified_comment_for_ticket(msgs, check_tid):
            return (
                f"本会话 ticket_id={check_tid} 已有 verified=true 写评，"
                f"禁止再次 create_ticket_comment（防双发）"
            )

    if name in (
        "change_ticket_status",
        "list_ticket_comments",
        "create_ticket_attachment",
        "get_ticket",
    ):
        req_tid = str(
            args.get("ticket_id") or args.get("ticketId") or ""
        ).strip()
        if req_tid and not is_numeric_ticket_id(req_tid) and name != "get_ticket":
            # get_ticket 有时可用 code；写评/改状态/列表评论必须数字 id
            return (
                f"{name} 的 ticket_id 必须用数字 id，禁止「{req_tid}」。"
                f"用工单 get_ticket.id（如 215367），不要用 SH20… 编号。"
            )

    if name == "change_ticket_status":
        try:
            status_int = int(args.get("status"))
        except (TypeError, ValueError):
            status_int = None
        if status_int == 3:
            expected = expected_tid
            req_tid = str(
                args.get("ticket_id") or args.get("ticketId") or ""
            ).strip()
            check_tid = expected or req_tid
            if check_tid and is_numeric_ticket_id(check_tid):
                for text in extract_verified_comment_texts(msgs, check_tid):
                    if is_unresolved_suggestion_text(text):
                        return (
                            f"评论仍为「请现场/请人工/无法远程」等未修复升级，"
                            f"禁止改 status=3；仅诊断≠闭环，请改 status=6"
                        )

    return None
