"""按 agent 的能力作用域（对齐 Claude Code subagent / Agent Skills 的通用 frontmatter）。

字段（写在 `PROMPT.md` / `TEAM.md` frontmatter，或由 `SubagentManager` 显式传入）:
- ``tools``           正向白名单（逗号串或列表）；省略/空 = 全部
- ``disallowedTools`` 黑名单，优先级高于白名单；兼容旧字段 ``tool_denylist``
- ``mcpServers``      允许连接的 MCP server 名列表；省略 = 继承(全部)，``[]`` = 无
- ``skills``          允许的技能白名单；省略/空 = 全部
- ``permissionMode``  覆盖权限模式（default/smart/auto/plan，可选）

约定：
- 核心工具（``skill``/``search_tools``/``whoami``/``ask_user``）**恒定可用**，仅 `disallowedTools` 可显式移除。
- 无任何字段 = 全量（零回归）。
- 能力只能收紧，不能突破 RBAC。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 恒定注入的核心工具（除非显式 disallow）
CORE_TOOLS = frozenset({"skill", "execute_skill", "search_tools", "whoami", "ask_user"})

_MCP_SERVER_KEYS = ("mcpServers", "mcp_servers")
_TOOLS_DENY_KEYS = ("disallowedTools", "disallowed_tools", "tool_denylist")
_PERMISSION_KEYS = ("permissionMode", "permission_mode")


def as_list(value: Any) -> list[str]:
    """把 frontmatter 值规范为字符串列表；容忍逗号串/列表/内联 MCP 定义(dict 取键名)。"""
    out: list[str] = []
    if value is None:
        return out
    if isinstance(value, str):
        value = value.split(",")
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                for key in item:
                    out.append(str(key).strip())
                    break
    return out


def _by_keys(fm: dict, keys: tuple[str, ...], default=None):
    for k in keys:
        if k in fm:
            return fm[k]
    return default


@dataclass
class Capabilities:
    tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    mcp_servers: list[str] | None = None
    skills: list[str] = field(default_factory=list)
    permission_mode: str = ""

    @property
    def is_empty(self) -> bool:
        """无任何限制（用于快速通道/零回归判定）。"""
        return (
            not self.tools
            and not self.disallowed_tools
            and self.mcp_servers is None
            and not self.skills
            and not self.permission_mode
        )

    # ── 判定 ─────────────────────────────────────────

    def allows_tool(self, name: str) -> bool:
        """非 MCP 工具（builtin / plugin / 核心）是否可用。"""
        if not name:
            return True
        if name in self.disallowed_tools:
            return False
        if not self.tools:
            return True
        return name in self.tools or name in CORE_TOOLS

    def allows_mcp_server(self, server: str | None) -> bool:
        """指定 MCP server 是否允许连接/暴露。"""
        if not server:
            return True
        if f"mcp__{server}" in self.disallowed_tools or "mcp__*" in self.disallowed_tools:
            return False
        if self.mcp_servers is None:
            return True
        return server in self.mcp_servers

    def allows_skill(self, name: str) -> bool:
        """指定技能是否可见/可调用。"""
        if not name:
            return True
        if not self.skills:
            return True
        return name in self.skills

    # ── 构造 ─────────────────────────────────────────

    @classmethod
    def from_frontmatter(cls, fm: dict | None) -> Capabilities:
        if not fm:
            return cls()
        raw_mcp = _by_keys(fm, _MCP_SERVER_KEYS, default=None)
        mcp_servers = None if raw_mcp is None else as_list(raw_mcp)
        return cls(
            tools=as_list(fm.get("tools")),
            disallowed_tools=as_list(_by_keys(fm, _TOOLS_DENY_KEYS, default=[])),
            mcp_servers=mcp_servers,
            skills=as_list(fm.get("skills")),
            permission_mode=str(_by_keys(fm, _PERMISSION_KEYS, default="") or "").strip(),
        )

    @classmethod
    def coerce(cls, value) -> Capabilities:
        """把 dict / Capabilities / None 统一为 Capabilities。"""
        if isinstance(value, Capabilities):
            return value
        if isinstance(value, dict):
            return cls.from_frontmatter(value)
        return cls()

    def override_with(self, member: Capabilities) -> Capabilities:
        """成员能力整体覆盖团队能力（成员非空则以其为准；否则继承团队）。"""
        if member is None or member.is_empty:
            return self
        return member
