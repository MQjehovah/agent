"""whoami 内置工具 — 返回当前用户身份画像(只读, 无需确认)。

仿 search_tools 注册进内置工具表(kind 语义=只读): 属核心工具恒注入,
不进渐进披露的远程工具检索。数据来源: 当前 run 的 RunContext + rbac_users
(见 agent.user_profile)。群聊下工具是显式调用, 仍返回触发人身份; 只含身份
字段, 不含记忆/敏感数据。
"""
import logging
from typing import Any

from . import BuiltinTool

logger = logging.getLogger("agent.tools")

_UNKNOWN = "未知"


class WhoamiTool(BuiltinTool):
    """返回当前用户画像(姓名/工号/部门/角色/渠道/用户 ID)。"""

    @property
    def name(self) -> str:
        return "whoami"

    @property
    def description(self) -> str:
        return (
            "返回当前用户的身份画像（姓名、工号、部门、角色、钉钉 userId、渠道、用户 ID）。"
            "用于回答“我是谁/我的工号/我的部门/我的角色/我的钉钉号”等个人身份问题；"
            "也可用于核对系统提示中注入的当前用户信息。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        from agent.core import current_run
        from agent.user_profile import resolve_user_profile

        profile = resolve_user_profile(current_run())
        role = profile["user_role"] or profile["role"]
        lines = [
            "用户身份：",
            f"- 姓名：{profile['name'] or _UNKNOWN}",
            f"- 工号：{profile['employee_id'] or _UNKNOWN}",
            f"- 部门：{profile['department'] or _UNKNOWN}",
            f"- 角色：{role or _UNKNOWN}",
            f"- 钉钉 userId：{profile.get('dingtalk') or _UNKNOWN}",
            f"- 渠道：{profile['channel'] or _UNKNOWN}",
            f"- 用户 ID：{profile['uid'] or _UNKNOWN}",
        ]
        return "\n".join(lines)
