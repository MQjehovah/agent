from enum import Enum
from dataclasses import dataclass, field
from typing import Any


class HookEvent(Enum):
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    AGENT_START = "agent_start"
    AGENT_STOP = "agent_stop"
    SESSION_CREATE = "session_create"
    SESSION_CLOSE = "session_close"
    CHAT_EVENT = "chat_event"
    TOOL_START = "tool_start"
    TOOL_RESULT = "tool_result"
    ROUND_START = "round_start"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_RESULT = "subagent_result"
    SUBAGENT_CHAT_EVENT = "subagent_chat_event"
    SUBAGENT_TOOL_START = "subagent_tool_start"
    SUBAGENT_TOOL_RESULT = "subagent_tool_result"
    SUBAGENT_ROUND_START = "subagent_round_start"


@dataclass
class HookContext:
    event: HookEvent
    tool_name: str = ""
    arguments: dict = field(default_factory=dict)
    result: Any = None
    error: Exception = None
    metadata: dict = field(default_factory=dict)
    token: str = ""
    agent_name: str = ""
    agent_type: str = ""
