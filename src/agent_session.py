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
    max_iterations: int = 100
    _agent: Optional["Agent"] = field(default=None, repr=False)

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

    async def think(self) -> Any:
        if not self._agent:
            raise RuntimeError("Session not bound to Agent")
        
        response = await self._agent._think(self.messages)
        return response

    async def execute_tool(self, name: str, args: Dict) -> str:
        if not self._agent:
            return "Agent未连接"
        return await self._agent.mcp.call_tool(name, args)

    async def run(self, task: str) -> str:
        self.add_message("user", task)
        logger.info(f"[Session {self.session_id}] 开始执行任务: {task}")

        for i in range(self.max_iterations):
            response = await self.think()

            msg = response.choices[0].message
            self.add_message(
                "assistant",
                msg.content or "",
                tool_calls=[{
                    "id": tc.id,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                }] if msg.tool_calls else None
            )

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    func = tc.function
                    if not func or not func.name:
                        continue

                    try:
                        args = (func.arguments if isinstance(func.arguments, dict) 
                               else json.loads(func.arguments))
                    except:
                        args = {}

                    logger.info(f"[Session {self.session_id}] → 调用工具: {func.name}")
                    result = await self.execute_tool(func.name, args)
                    logger.info(f"[Session {self.session_id}] ✓ {func.name} 执行完成")

                    self.add_message("tool", result, tool_call_id=tc.id)

                continue

            if msg.content and msg.content.strip():
                logger.info(f"[Session {self.session_id}] 任务完成")
                return msg.content

        logger.warning(f"[Session {self.session_id}] 达到最大迭代次数")
        return "达到最大迭代次数"


import json


class AgentSessionManager:
    def __init__(self, agent: "Agent"):
        self.agent = agent
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
                max_iterations=self.agent.max_iterations
            )
            session._agent = self.agent
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

    async def run_in_session(
        self, 
        session_id: str, 
        task: str,
        system_prompt: str = ""
    ) -> str:
        session = await self.get_session(session_id)
        if not session:
            session = await self.create_session(session_id, system_prompt)
        
        return await session.run(task)

    def list_sessions(self) -> List[str]:
        return list(self.sessions.keys())
