"""
Agent 运行上下文 — RunContext + current_run()

单次 agent.run() 的执行上下文。通过 contextvars.ContextVar 绑定到当前 asyncio Task，
多个并发 run() 各自拥有独立上下文，消除实例属性竞态。
"""
import contextvars
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger("agent.agent")


@dataclass
class AgentResult:
    agent_id: str
    status: str
    result: str
    completed_at: str = field(
        default_factory=lambda: datetime.now().isoformat())


@dataclass
class RunContext:
    """单次 agent.run() 的执行上下文。

    通过 contextvars.ContextVar 绑定到当前 asyncio Task，多个并发 run()
    各自拥有独立上下文。
    """
    user_id: str = ""
    user_name: str = ""
    role: str = "default"
    session: Any = None
    task: str = ""
    consecutive_errors: int = 0
    retry_context: str = ""
    status: str = "pending"
    result: str = ""
    run_id: str = ""
    system_prompt: str = ""
    prompt_builder: Any = None
    agent_id: str = ""
    system_static: str = ""
    system_dynamic: str = ""
    task_dir: str = ""


_current_run: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar(
    "agent_current_run", default=None
)
_EMPTY_RUN = RunContext()


def current_run() -> RunContext:
    """获取当前 run() 的执行上下文（并发安全）。run() 之外调用返回空上下文（只读）。"""
    return _current_run.get() or _EMPTY_RUN
