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


@dataclass
class HookContext:
    event: HookEvent
    tool_name: str = ""
    arguments: dict = field(default_factory=dict)
    result: Any = None
    error: Exception = None
    metadata: dict = field(default_factory=dict)
