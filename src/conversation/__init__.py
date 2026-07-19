"""
Conversation — 会话、消息压缩、提示词构建

独立于 agent 核心，管理对话生命周期、上下文压缩、prompt 组装。
"""

from conversation.prompt import PromptBuilder, PromptSection
from conversation.session import AgentSession, AgentSessionManager

cleanup_orphaned_tool_calls = AgentSessionManager.cleanup_orphaned_tool_calls
sliding_window = AgentSessionManager.sliding_window
tool_collapse = AgentSessionManager.tool_collapse
context_collapse = AgentSessionManager.context_collapse
compress_if_needed = AgentSessionManager.compress_if_needed

__all__ = [
    "AgentSession", "AgentSessionManager",
    "cleanup_orphaned_tool_calls", "sliding_window", "tool_collapse",
    "context_collapse", "compress_if_needed",
    "PromptBuilder", "PromptSection",
]
