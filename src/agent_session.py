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
    MAX_CONTEXT_TOKENS = 100 * 1000  # 上下文 token 上限（按模型调整）
    # microcompact 参数
    KEEP_RECENT_TOOL_RESULTS = 5       # 保留最近 N 条工具结果的完整内容
    TOOL_RESULT_COLLAPSE_CHARS = 300   # 旧工具结果截断到多少字符
    # context collapse 参数
    TEXT_BLOCK_COLLAPSE_THRESHOLD = 3000  # 超过此长度的文本块被折叠
    TEXT_BLOCK_COLLAPSE_HEAD = 500        # 折叠后保留头部字符数
    TEXT_BLOCK_COLLAPSE_TAIL = 300        # 折叠后保留尾部字符数

    @staticmethod
    def estimate_tokens(messages: list, tool_defs: list = None) -> int:
        """估算上下文 token 数。

        基于序列化后的 UTF-8 字节数估算。OpenAI 的 cl100k_base tokenizer
        大致为 ~3.5 字节/token（中英混合内容），取 3 保守估计以确保
        不会低估上下文大小。

        包含：消息完整 JSON 结构 + 工具定义。
        """
        BYTES_PER_TOKEN = 3.5

        payload_bytes = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))

        if tool_defs:
            payload_bytes += len(json.dumps(tool_defs, ensure_ascii=False).encode("utf-8"))

        return max(1, int(payload_bytes / BYTES_PER_TOKEN))

    @staticmethod
    def microcompact(messages: list) -> list:
        """轻量级压缩：将旧的工具结果截断，零成本不调用 LLM。

        策略：
        - 保留最近 KEEP_RECENT_TOOL_RESULTS 条工具结果不变
        - 更早的工具结果截断到 TOOL_RESULT_COLLAPSE_CHARS 字符
        """
        # 先收集所有 tool 消息的索引
        tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]

        if len(tool_indices) <= AgentSessionManager.KEEP_RECENT_TOOL_RESULTS:
            return messages

        # 需要截断的旧 tool 消息索引（排除最近 N 条）
        old_tool_indices = tool_indices[:-AgentSessionManager.KEEP_RECENT_TOOL_RESULTS]

        result = list(messages)  # 浅拷贝
        modified = False
        for idx in old_tool_indices:
            msg = result[idx]
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > AgentSessionManager.TOOL_RESULT_COLLAPSE_CHARS:
                # 获取工具名
                tool_name = msg.get("name", "unknown")
                truncated = (
                    f"[旧结果已压缩 | 工具: {tool_name} | "
                    f"原始 {len(content)} 字符]\n"
                    f"{content[:AgentSessionManager.TOOL_RESULT_COLLAPSE_CHARS]}..."
                )
                result[idx] = {**msg, "content": truncated}
                modified = True

        if modified:
            logger.debug(f"microcompact: 截断了 {len(old_tool_indices)} 条旧工具结果")
        return result

    @staticmethod
    def context_collapse(messages: list) -> list:
        """中等压缩：折叠超长文本块（不调用 LLM）。

        对超过阈值的长消息，只保留头部 + 尾部 + 省略号。
        """
        threshold = AgentSessionManager.TEXT_BLOCK_COLLAPSE_THRESHOLD
        head = AgentSessionManager.TEXT_BLOCK_COLLAPSE_HEAD
        tail = AgentSessionManager.TEXT_BLOCK_COLLAPSE_TAIL

        result = list(messages)
        modified = False
        # 跳过最后 4 条消息（最近上下文保持完整）
        for i in range(len(result) - 4):
            msg = result[i]
            # 不折叠系统提示
            if msg.get("role") == "system":
                continue
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > threshold:
                collapsed = (
                    f"{content[:head]}\n"
                    f"... [省略了 {len(content) - head - tail} 字符] ...\n"
                    f"{content[-tail:]}"
                )
                result[i] = {**msg, "content": collapsed}
                modified = True

        if modified:
            logger.debug("context_collapse: 折叠了超长文本块")
        return result

    @staticmethod
    async def compress_if_needed(
        messages: list,
        llm_client,
        max_tokens: int = None,
        tool_defs: list = None
    ) -> list:
        """三层渐进式上下文压缩。

        Layer 1 (50%): microcompact — 截断旧工具结果，零成本
        Layer 2 (65%): context_collapse — 折叠超长文本块，零成本
        Layer 3 (80%): LLM 压缩 — 调用模型生成摘要，有成本
        """
        max_tokens = max_tokens or AgentSessionManager.MAX_CONTEXT_TOKENS
        token_count = AgentSessionManager.estimate_tokens(messages, tool_defs)

        if token_count < max_tokens * 0.5:
            return messages

        # Layer 1: microcompact — 截断旧工具结果
        if token_count >= max_tokens * 0.5:
            messages = AgentSessionManager.microcompact(messages)
            token_count = AgentSessionManager.estimate_tokens(messages, tool_defs)
            if token_count < max_tokens * 0.65:
                return messages

        # Layer 2: context_collapse — 折叠超长文本块
        if token_count >= max_tokens * 0.65:
            messages = AgentSessionManager.context_collapse(messages)
            token_count = AgentSessionManager.estimate_tokens(messages, tool_defs)
            if token_count < max_tokens * 0.8:
                return messages

        # Layer 3: LLM 压缩 — 生成结构化摘要
        logger.info(f"上下文估计 {token_count} tokens，接近上限 {max_tokens}，启动 LLM 压缩...")

        system_msgs = [m for m in messages if m.get("role") == "system"]
        recent_msgs = messages[-8:]  # 保留最近 4 轮（user+assistant+tool 可能占多条）
        history_msgs = [
            m for m in messages
            if m not in system_msgs and m not in recent_msgs
        ]

        if not history_msgs:
            return messages

        # 序列化历史，限制输入长度
        history_text = ""
        for m in history_msgs:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            name = m.get("name", "")
            prefix = f"[{role}{'/' + name if name else ''}]"
            if content:
                # 每条消息最多取 1000 字符
                snippet = content[:1000] + ("..." if len(content) > 1000 else "")
                history_text += f"{prefix} {snippet}\n"

        if not history_text.strip():
            return messages

        try:
            summary_prompt = (
                "请将以下 Agent 对话历史压缩为结构化摘要，保留以下信息：\n"
                "1. 用户的核心请求和意图\n"
                "2. 已完成的关键操作和结论\n"
                "3. 发现的文件和代码关键位置（文件路径:行号）\n"
                "4. 未完成的任务\n"
                "5. 当前工作进度\n\n"
                f"对话历史：\n{history_text[:10000]}"
            )
            response = await llm_client.chat(
                messages=[
                    {"role": "system", "content": "你是上下文压缩助手。输出简洁的中文结构化摘要。"},
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

            new_count = AgentSessionManager.estimate_tokens(compressed, tool_defs)
            logger.info(f"上下文压缩完成: ~{token_count} → ~{new_count} tokens")
            return compressed
        except Exception as e:
            logger.warning(f"LLM 上下文压缩失败，保留原始消息: {e}")
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
            try:
                await asyncio.sleep(CLEANUP_INTERVAL)
            except asyncio.CancelledError:
                logger.info("会话清理循环被取消")
                return
            try:
                await self._cleanup_expired_sessions()
            except asyncio.CancelledError:
                logger.info("会话清理被取消")
                return
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