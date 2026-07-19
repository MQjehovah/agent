"""
Conversation — 会话、消息压缩、提示词构建

独立于 agent 核心，管理对话生命周期、上下文压缩、prompt 组装。
"""

from conversation.session import AgentSession, AgentSessionManager
from conversation.compression import (
    cleanup_orphaned_tool_calls, sliding_window, tool_collapse,
    context_collapse, compress_if_needed,
)
from conversation.prompt import PromptBuilder, PromptSection

__all__ = [
    "AgentSession", "AgentSessionManager",
    "cleanup_orphaned_tool_calls", "sliding_window", "tool_collapse",
    "context_collapse", "compress_if_needed",
    "PromptBuilder", "PromptSection",
]
