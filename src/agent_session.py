import uuid
import asyncio
import json
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from openai.types.chat import ChatCompletionMessageParam

from config import Config

logger = logging.getLogger("agent.session")


@dataclass
class AgentSession:
    agent_id: str = ""
    session_id: str = ""
    system_prompt: str = ""
    messages: List[ChatCompletionMessageParam] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    last_accessed: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        if self.system_prompt:
            self.reset()

    def reset(self):
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.last_accessed = datetime.now()

    def add_message(self, role: str, content: str, **kwargs):
        msg: Dict[str, Any] = {"role": role, "content": content or ""}
        if kwargs:
            msg.update(kwargs)
        self.messages.append(msg)
        self.last_accessed = datetime.now()

        try:
            from storage import get_storage
            storage = get_storage()
            if storage and self.session_id:
                storage.save_message(
                    agent_id=self.agent_id,
                    session_id=self.session_id,
                    role=role,
                    content=content or "",
                    tool_calls=kwargs.get("tool_calls"),
                    tool_call_id=kwargs.get("tool_call_id"),
                    name=kwargs.get("name")
                )
        except ImportError:
            pass

    def is_expired(self, ttl_seconds: int = None) -> bool:
        """检查会话是否过期"""
        ttl = ttl_seconds or Config.SESSION_TTL_SECONDS
        return datetime.now() - self.last_accessed > timedelta(seconds=ttl)

    def touch(self):
        """更新最后访问时间"""
        self.last_accessed = datetime.now()


class AgentSessionManager:
    MAX_ITERATIONS = 100
    CLEANUP_INTERVAL = 300  # 清理间隔: 5分钟
    MAX_CONTEXT_TOKENS = 100000  # 上下文 token 上限（按模型调整）

    @staticmethod
    def estimate_tokens(messages: list) -> int:
        """粗略估算消息列表的 token 数（中文约 1.5 字/token）"""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += int(len(content) / 1.5)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        total += int(len(part.get("text", "")) / 1.5)
            # 工具调用
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    total += int(len(json.dumps(func.get("arguments", ""), ensure_ascii=False)) / 1.5)
        return total

    @staticmethod
    async def compress_if_needed(
        messages: list,
        llm_client,
        max_tokens: int = None
    ) -> list:
        """如果上下文过大，压缩历史消息"""
        max_tokens = max_tokens or AgentSessionManager.MAX_CONTEXT_TOKENS
        token_count = AgentSessionManager.estimate_tokens(messages)

        if token_count < max_tokens * 0.8:
            return messages

        logger.info(f"上下文估计 {token_count} tokens，接近上限 {max_tokens}，开始压缩...")

        # 保留系统消息 + 最近 6 条消息
        system_msgs = [m for m in messages if m.get("role") == "system"]
        recent_msgs = messages[-6:]
        history_msgs = [
            m for m in messages
            if m not in system_msgs and m not in recent_msgs
        ]

        if not history_msgs:
            return messages

        # 序列化历史用于压缩
        history_text = ""
        for m in history_msgs:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            if content:
                history_text += f"[{role}] {content}\n"

        if not history_text.strip():
            return messages

        try:
            summary_prompt = (
                "你是一个对话压缩助手。请将以下对话历史压缩为简洁摘要，"
                "保留关键决策、结论和未完成任务。\n\n"
                f"对话历史：\n{history_text[:8000]}"
            )
            response = await llm_client.chat(
                messages=[
                    {"role": "system", "content": "你是对话压缩助手，输出简洁的中文摘要。"},
                    {"role": "user", "content": summary_prompt}
                ],
                tools=None,
                stream=False,
                use_cache=False
            )
            summary = response.choices[0].message.content or ""

            compressed = [
                *system_msgs,
                {"role": "user", "content": f"[对话历史摘要]\n{summary}"},
                {"role": "assistant", "content": "已了解历史上下文，请继续。"},
                *recent_msgs,
            ]

            new_count = AgentSessionManager.estimate_tokens(compressed)
            logger.info(f"上下文压缩完成: ~{token_count} → ~{new_count} tokens")
            return compressed
        except Exception as e:
            logger.warning(f"上下文压缩失败，保留原始消息: {e}")
            return messages

    def __init__(self, ttl_seconds: int = None, max_sessions: int = None):
        self.sessions: Dict[str, AgentSession] = {}
        self._lock = asyncio.Lock()
        self.ttl_seconds = ttl_seconds or Config.SESSION_TTL_SECONDS
        self.max_sessions = max_sessions or Config.MAX_SESSIONS
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start_cleanup_task(self):
        """启动定期清理任务"""
        if self._cleanup_task:
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("会话清理任务已启动")

    def stop_cleanup_task(self):
        """停止清理任务"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None
            logger.info("会话清理任务已停止")

    async def _cleanup_loop(self):
        """定期清理过期会话"""
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            try:
                await self._cleanup_expired_sessions()
            except Exception as e:
                logger.error(f"会话清理失败: {e}")

    async def _cleanup_expired_sessions(self):
        """清理过期和超出限制的会话"""
        async with self._lock:
            # 清理过期会话
            expired = [
                sid for sid, session in self.sessions.items()
                if session.is_expired(self.ttl_seconds)
            ]
            for sid in expired:
                del self.sessions[sid]
                logger.debug(f"清理过期会话: {sid}")

            if expired:
                logger.info(f"已清理 {len(expired)} 个过期会话")

            # 如果超出限制，清理最旧的会话
            if len(self.sessions) > self.max_sessions:
                sorted_sessions = sorted(
                    self.sessions.items(),
                    key=lambda x: x[1].last_accessed
                )
                to_remove = len(self.sessions) - self.max_sessions
                for sid, _ in sorted_sessions[:to_remove]:
                    del self.sessions[sid]
                    logger.debug(f"清理超出限制会话: {sid}")
                logger.info(f"已清理 {to_remove} 个超出限制会话")

    async def create_session(
        self,
        session_id: Optional[str] = None,
        system_prompt: str = "",
        agent_id: str = ""
    ) -> AgentSession:
        if not session_id:
            session_id = str(uuid.uuid4())

        async with self._lock:
            # 如果超出限制，先清理
            if len(self.sessions) >= self.max_sessions:
                await self._cleanup_expired_sessions()

            if session_id in self.sessions:
                session = self.sessions[session_id]
                session.touch()
                return session

            session = AgentSession(
                session_id=session_id,
                agent_id=agent_id,
                system_prompt=system_prompt,
            )
            self.sessions[session_id] = session
            return session

    async def get_session(self, session_id: str) -> Optional[AgentSession]:
        session = self.sessions.get(session_id)
        if session:
            session.touch()
        return session

    async def remove_session(self, session_id: str):
        async with self._lock:
            if session_id in self.sessions:
                del self.sessions[session_id]
                logger.info(f"删除Session: {session_id}")

    def list_sessions(self) -> List[str]:
        return list(self.sessions.keys())

    def get_session_count(self) -> int:
        return len(self.sessions)

    def get_session_info(self) -> Dict[str, Any]:
        """获取会话统计信息"""
        return {
            "total": len(self.sessions),
            "max_sessions": self.max_sessions,
            "ttl_seconds": self.ttl_seconds,
            "sessions": [
                {
                    "id": sid,
                    "agent_id": s.agent_id,
                    "messages": len(s.messages),
                    "last_accessed": s.last_accessed.isoformat(),
                    "expired": s.is_expired(self.ttl_seconds)
                }
                for sid, s in self.sessions.items()
            ]
        }