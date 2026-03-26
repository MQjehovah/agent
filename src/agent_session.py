import uuid
import asyncio
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from openai.types.chat import ChatCompletionMessageParam

logger = logging.getLogger("agent.session")


@dataclass
class AgentSession:
    session_id: str
    system_prompt: str = ""
    messages: List[ChatCompletionMessageParam] = field(default_factory=list)

    def __post_init__(self):
        if self.system_prompt:
            self.reset()

    def reset(self):
        self.messages = [{"role": "system", "content": self.system_prompt}]

    def add_message(self, role: str, content: str, **kwargs):
        msg: Dict[str, Any] = {"role": role, "content": content or ""}
        if kwargs:
            msg.update(kwargs)
        self.messages.append(msg)


class AgentSessionManager:
    MAX_ITERATIONS = 100

    def __init__(self):
        self.sessions: Dict[str, AgentSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        session_id: Optional[str] = None,
        system_prompt: str = ""
    ) -> AgentSession:
        if not session_id:
            session_id = str(uuid.uuid4())

        async with self._lock:
            if session_id in self.sessions:
                return self.sessions[session_id]

            session = AgentSession(
                session_id=session_id,
                system_prompt=system_prompt,
            )
            self.sessions[session_id] = session
            logger.info(f"创建新Session: {session_id}")
            return session

    async def get_session(self, session_id: str) -> Optional[AgentSession]:
        return self.sessions.get(session_id)

    async def remove_session(self, session_id: str):
        async with self._lock:
            if session_id in self.sessions:
                del self.sessions[session_id]
                logger.info(f"删除Session: {session_id}")

    def list_sessions(self) -> List[str]:
        return list(self.sessions.keys())
