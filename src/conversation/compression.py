"""
上下文压缩 — 4 层渐进式压缩管线

方法都在 AgentSessionManager 类上，此类提供便捷引用。
"""
from conversation.session import AgentSessionManager

cleanup_orphaned_tool_calls = AgentSessionManager.cleanup_orphaned_tool_calls
sliding_window = AgentSessionManager.sliding_window
tool_collapse = AgentSessionManager.tool_collapse
context_collapse = AgentSessionManager.context_collapse
compress_if_needed = AgentSessionManager.compress_if_needed

__all__ = [
    "cleanup_orphaned_tool_calls",
    "sliding_window",
    "tool_collapse",
    "context_collapse",
    "compress_if_needed",
]
