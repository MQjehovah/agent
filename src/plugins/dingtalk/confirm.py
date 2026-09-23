"""钉钉互动卡片工具确认回路(纯逻辑, 可离线单测)。

调用链: 工具执行器命中「需要用户确认」→ ``agent.on_confirm`` → 本模块 confirmer
→ ``send_card``(发互动卡片给触发人私聊) → ``wait_action``(等按钮回调/超时)
→ 返回 bool 裁决。**fail-closed**: 超时、发送失败、回调解析失败一律 False。

设计约束:
- 副作用全部经注入函数(``send_card``/``wait_action``/``audit``)完成; 本模块不触网、
  不依赖 dingtalk_stream / httpx, 因此可以完全离线复现各分支;
- 同一 request_id 只认首个裁决(重复点击幂等, 见 ``CardActionRegistry``);
- 卡片参数值遵守钉钉单值 1KB 上限, 工具参数摘要截断(``MAX_CARD_PARAM_CHARS``)。

钉钉实现依据(dingtalk-stream 0.24.3):
- 卡片回调 topic 常量 ``dingtalk_stream.Card_Callback_Router_Topic``
  = ``/v1.0/card/instances/callback``(见 SDK ``card_callback.py``);
- 回调消息 ``CardCallbackMessage.from_dict`` 解析字段 ``outTrackId``(卡片实例 id)/
  ``userId``(点击人)/``content``(JSON 字符串, 含 ``cardPrivateData``);
- 按钮动作: ``content.cardPrivateData.actionIds[0]``(模板按钮) 或
  ``content.cardPrivateData.params.action|id``(通用卡片布局按钮)。
"""
import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable

logger = logging.getLogger("plugin.dingtalk.confirm")

CONFIRM_TIMEOUT_ENV = "DINGTALK_CONFIRM_TIMEOUT"
CONFIRM_CARD_TEMPLATE_ENV = "DINGTALK_CONFIRM_CARD_TEMPLATE_ID"

# 默认确认超时(秒); 环境变量非法(非数字/<=0)时回退该值
DEFAULT_CONFIRM_TIMEOUT = 120.0

# 钉钉卡片平台「通用 AI 卡片」发布模板: 支持发送时用 sys_full_json_obj 传按钮布局,
# 按钮 request=true 时点击经 STREAM 回调(outTrackId 原样回传)。
DEFAULT_CARD_TEMPLATE_ID = "382e4302-551d-4880-bf29-a30acfab2e71.schema"

CONFIRM_TITLE = "工具执行确认"
CONFIRM_UNAVAILABLE_REPLY = "该操作需确认但当前渠道不可用，已拒绝"

# outTrackId 前缀: 回调据此还原 request_id(卡片实例 id 全局唯一, 不信任回调里的其它字段)
OUT_TRACK_PREFIX = "dtconfirm:"
AGREE_ACTION_ID = "agree"
REJECT_ACTION_ID = "reject"

# 卡片 cardParamMap 单值上限 1KB(留出 JSON 转义余量)
MAX_CARD_PARAM_CHARS = 900
DEFAULT_ARG_SUMMARY_CHARS = 280

_APPROVE_WORDS = frozenset({"agree", "allow", "approve", "approved", "accept", "confirm", "同意", "允许"})
_REJECT_WORDS = frozenset({"reject", "deny", "denied", "refuse", "refused", "拒绝"})


def resolve_confirm_timeout(value=None, default: float = DEFAULT_CONFIRM_TIMEOUT) -> float:
    """解析确认超时(秒): 显式值 > ``DINGTALK_CONFIRM_TIMEOUT`` > 默认; 非法回退默认。"""
    raw = value if value is not None else os.environ.get(CONFIRM_TIMEOUT_ENV)
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return float(default)
    return parsed if parsed > 0 else float(default)


def parse_out_track_id(value) -> str:
    """从回调 outTrackId 还原 request_id; 非本插件卡片返回空串。"""
    text = str(value or "")
    if text.startswith(OUT_TRACK_PREFIX):
        return text[len(OUT_TRACK_PREFIX):]
    return ""


def parse_card_action(content) -> "bool | None":
    """解析卡片回调 ``content``: True=同意 / False=拒绝 / None=无关或无效。

    兼容两种形态: 模板按钮经 ``cardPrivateData.actionIds``, 通用卡片布局按钮经
    ``cardPrivateData.params.action|id``; 未知动作(如 openLink)一律 None(不裁决)。
    """
    data = content
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    private = data.get("cardPrivateData")
    if not isinstance(private, dict):
        # 少数集成会把 cardPrivateData 内层直接平铺传入
        private = data if ("actionIds" in data or "params" in data) else {}
    action = ""
    params = private.get("params")
    if isinstance(params, dict):
        action = str(params.get("action") or params.get("id") or "").strip().lower()
    if not action:
        action_ids = private.get("actionIds")
        if isinstance(action_ids, list) and action_ids:
            action = str(action_ids[0] or "").strip().lower()
    if action in _APPROVE_WORDS:
        return True
    if action in _REJECT_WORDS:
        return False
    return None


def _arg_summary(args, max_chars: int) -> str:
    try:
        text = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(args)
    return text[:max_chars] + "…" if len(text) > max_chars else text


def build_confirm_card_payload(tool: str, args: dict, *, request_id: str,
                               context: str = "",
                               max_arg_chars: int = DEFAULT_ARG_SUMMARY_CHARS) -> "dict[str, str]":
    """构建互动卡片 cardParamMap(纯函数)。

    - ``msgTitle``/``staticMsgContent``: 通用 AI 卡片直接渲染的标题与正文
      (工具名 + 参数摘要截断 + 连接器/会话上下文);
    - ``sys_full_json_obj``: 按钮布局(同意/拒绝, ``request=true`` 触发 STREAM 回调),
      按钮 id 即裁决值;
    - ``flowStatus=3``: 卡片直接进入「已渲染」态;
    - ``tool``/``argsDigest``/``requestId``/``context``: 供自建卡片模板绑定的冗余字段。
    """
    summary = _arg_summary(args, max_arg_chars)
    lines = [f"工具：{tool}", f"参数：{summary}"]
    if context:
        lines.append(f"上下文：{context}")
    body = "\n\n".join(lines)
    if len(body) > MAX_CARD_PARAM_CHARS:
        body = body[:MAX_CARD_PARAM_CHARS] + "…"
    layout = json.dumps({
        "order": ["msgTitle", "staticMsgContent", "msgButtons"],
        "msgButtons": [
            {"text": "同意", "color": "blue", "id": AGREE_ACTION_ID, "request": True},
            {"text": "拒绝", "color": "gray", "id": REJECT_ACTION_ID, "request": True},
        ],
    }, ensure_ascii=False)
    return {
        "msgTitle": CONFIRM_TITLE,
        "staticMsgContent": body,
        "sys_full_json_obj": layout,
        "flowStatus": "3",
        "tool": str(tool),
        "argsDigest": summary,
        "requestId": str(request_id),
        "context": str(context or ""),
    }


class CardActionRegistry:
    """request_id → Future 的待裁决登记表。

    - ``resolve`` 只接受首个裁决(重复点击/未知 request 返回 False, 幂等);
    - ``wait`` 超时返回 None(由 confirmer 记 timeout 并拒绝, fail-closed);
    - 超时后到达的迟到回调因登记已被清理被忽略。
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future] = {}

    def register(self, request_id: str) -> "asyncio.Future":
        future = asyncio.get_running_loop().create_future()
        self._pending[str(request_id)] = future
        return future

    def resolve(self, request_id: str, approved: bool) -> bool:
        future = self._pending.get(str(request_id))
        if future is None or future.done():
            return False
        future.set_result(bool(approved))
        return True

    def cancel(self, request_id: str) -> None:
        future = self._pending.pop(str(request_id), None)
        if future is not None and not future.done():
            future.cancel()

    async def wait(self, request_id: str, timeout: float) -> "bool | None":
        future = self._pending.get(str(request_id))
        if future is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending.pop(str(request_id), None)


def create_card_confirmer(
    *,
    send_card: "Callable[[str, dict], Awaitable[bool]]",
    wait_action: "Callable[[str, float], Awaitable[bool | None]]",
    audit: "Callable[[str, bool, str], None]",
    timeout_seconds: "float | None" = None,
    now: "Callable[[], float] | None" = None,
    context: str = "",
    max_arg_chars: int = DEFAULT_ARG_SUMMARY_CHARS,
    request_id_factory: "Callable[[], str] | None" = None,
) -> "Callable[[str, dict], Awaitable[bool]]":
    """创建 ``agent.on_confirm`` 回调: ``async (tool, args) -> bool``。

    Args:
        send_card: 发送确认卡片, 返回 False 表示发送/渠道不可用(fail-closed);
        wait_action: 等待按钮回调, None=超时/未获得裁决;
        audit: 审计回调 ``(tool, ok, detail)``, detail ∈ approved/rejected/timeout/send_failed;
        timeout_seconds: 显式超时; None 时读 ``DINGTALK_CONFIRM_TIMEOUT``(非法回退 120);
        now: 时钟注入(默认 ``time.time``), 供 request_id/测试使用;
        context: 卡片正文附带的连接器/会话上下文;
    """
    clock = now or time.time
    timeout = resolve_confirm_timeout(timeout_seconds)

    def _new_request_id() -> str:
        if request_id_factory is not None:
            return str(request_id_factory())
        return f"{int(clock() * 1000):x}{uuid.uuid4().hex[:8]}"

    def _emit(tool: str, ok: bool, detail: str) -> None:
        try:
            audit(tool, bool(ok), detail)
        except Exception as e:  # 审计失败不影响裁决
            logger.warning(f"tool_confirm 审计回调失败(忽略): {e!r}")

    async def confirm(tool: str, args: dict) -> bool:
        request_id = _new_request_id()
        payload = build_confirm_card_payload(
            tool, args, request_id=request_id, context=context, max_arg_chars=max_arg_chars)
        try:
            sent = await send_card(request_id, payload)
        except Exception as e:
            logger.error(f"确认卡片发送异常: {e!r}")
            sent = False
        if not sent:
            _emit(tool, False, "send_failed")
            return False
        try:
            verdict = await wait_action(request_id, timeout)
        except Exception as e:
            logger.error(f"确认等待异常: {e!r}")
            verdict = None
        if verdict is True:
            _emit(tool, True, "approved")
            return True
        _emit(tool, False, "rejected" if verdict is False else "timeout")
        return False

    confirm.timeout_seconds = timeout  # type: ignore[attr-defined] — 便于观测/测试
    return confirm
