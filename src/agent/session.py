import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from openai.types.chat import ChatCompletionMessageParam

from config import Config

logger = logging.getLogger("agent.session")


@dataclass
class AgentSession:
    agent_id: str = ""
    session_id: str = ""
    system_prompt: str = ""
    messages: list[ChatCompletionMessageParam] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    last_accessed: datetime = field(default_factory=datetime.now)
    user_id: str = ""
    user_name: str = ""
    role: str = ""

    def __post_init__(self):
        if self.system_prompt:
            self.reset()

    def reset(self):
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.last_accessed = datetime.now()

    def add_message(self, role: str, content: str, **kwargs):
        msg: dict[str, Any] = {"role": role, "content": content or ""}
        if kwargs:
            msg.update(kwargs)
        self.messages.append(msg)
        self.last_accessed = datetime.now()

        try:
            from storage.storage import get_storage
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
    MAX_CONTEXT_TOKENS = int(os.environ.get("MAX_CONTEXT_TOKENS", 100 * 1000)) # 上下文 token 上限（按模型调整）
    KEEP_RECENT_TOOL_RESULTS = int(os.environ.get("KEEP_RECENT_TOOL_RESULTS", 5)) # 保留最近 N 条工具结果的完整内容
    TOOL_RESULT_COLLAPSE_CHARS = int(os.environ.get("TOOL_RESULT_COLLAPSE_CHARS", 300))  # 旧工具结果截断到多少字符
    TEXT_BLOCK_COLLAPSE_THRESHOLD = int(os.environ.get("TEXT_BLOCK_COLLAPSE_THRESHOLD", 3000)) # 超过此长度的文本块被折叠
    TEXT_BLOCK_COLLAPSE_HEAD = int(os.environ.get("TEXT_BLOCK_COLLAPSE_HEAD", 500)) # 折叠后保留头部字符数
    TEXT_BLOCK_COLLAPSE_TAIL = int(os.environ.get("TEXT_BLOCK_COLLAPSE_TAIL", 300)) # 折叠后保留尾部字符数
    SLIDING_WINDOW_SIZE = int(os.environ.get("SLIDING_WINDOW_SIZE", 10))
    SLIDING_WINDOW_SUMMARY_MAX = int(os.environ.get("SLIDING_WINDOW_SUMMARY_MAX", 6000))

    @classmethod
    def load_config(cls):
        cls.SLIDING_WINDOW_SIZE = Config.SLIDING_WINDOW_SIZE
        cls.SLIDING_WINDOW_SUMMARY_MAX = Config.SLIDING_WINDOW_SUMMARY_MAX
        cls.KEEP_RECENT_TOOL_RESULTS = Config.KEEP_RECENT_TOOL_RESULTS
        cls.TOOL_RESULT_COLLAPSE_CHARS = Config.TOOL_RESULT_COLLAPSE_CHARS

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
    def tool_collapse(messages: list) -> list:
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
        _COMPRESS_TAG = "[旧结果已压缩"
        # 这些工具的结果需要跨轮次保留，不压缩
        _KEEP_TOOLS = {"skill", "execute_skill", "ask_user"}
        for idx in old_tool_indices:
            msg = result[idx]
            content = msg.get("content", "")
            tool_name = msg.get("name", "unknown")
            # 技能/交互结果不压缩（需要跨轮次参考）
            if tool_name in _KEEP_TOOLS:
                continue
            if isinstance(content, str) and len(content) > AgentSessionManager.TOOL_RESULT_COLLAPSE_CHARS:
                # 跳过已压缩的，避免嵌套压缩
                if content.startswith(_COMPRESS_TAG):
                    continue
                truncated = (
                    f"{_COMPRESS_TAG} | 工具: {tool_name} | "
                    f"原始 {len(content)} 字符]\n"
                    f"{content[:AgentSessionManager.TOOL_RESULT_COLLAPSE_CHARS]}..."
                )
                result[idx] = {**msg, "content": truncated}
                modified = True

        if modified:
            logger.debug(f"tool_collapse: 截断了 {len(old_tool_indices)} 条旧工具结果")
        return result

    @staticmethod
    def sliding_window(messages: list, window_size: int = None) -> list:
        """滑动窗口：始终只保留最近 N 条非系统消息，确保 tool_call / tool_response 完整配对。

        滑落的消息被序列化为摘要文本，作为一条 user 消息保留在最前面，
        让模型仍可参考早期对话要点，但不再占据完整上下文空间。
        """
        window_size = window_size or AgentSessionManager.SLIDING_WINDOW_SIZE

        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        if len(non_system) <= window_size:
            return messages

        # 从末尾取 window_size 条，向前扩展直到 tool_call 响应对完整
        kept = list(non_system[-window_size:])

        # 收集 kept 中 assistant tool_calls 的 id 集合
        required_tc_ids = set()
        for m in kept:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    tc_id = tc.get("id", "")
                    if tc_id:
                        required_tc_ids.add(tc_id)

        # 检查 kept 中是否已有对应 tool response
        responded_ids = set()
        for m in kept:
            if m.get("role") == "tool" and m.get("tool_call_id"):
                responded_ids.add(m["tool_call_id"])

        # 向前扩展以补齐缺失的 tool response
        start_idx = len(non_system) - window_size - 1
        missing = required_tc_ids - responded_ids
        while missing and start_idx >= 0:
            m = non_system[start_idx]
            kept.insert(0, m)
            if m.get("role") == "tool" and m.get("tool_call_id") in missing:
                missing.discard(m["tool_call_id"])
            start_idx -= 1

        # 清理孤儿 tool_response：其 tool_calls 已被滑出窗口，留在 kept 中会破坏 LLM 配对要求
        tool_call_ids_in_kept = set()
        for m in kept:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    tc_id = tc.get("id", "")
                    if tc_id:
                        tool_call_ids_in_kept.add(tc_id)

        kept = [m for m in kept if not (
            m.get("role") == "tool"
            and m.get("tool_call_id")
            and m["tool_call_id"] not in tool_call_ids_in_kept
        )]

        # 滑落的消息 -> 摘要
        kept_set = set(id(m) for m in kept)
        evicted = [m for m in non_system if id(m) not in kept_set]

        if not evicted:
            return messages

        summary_parts = []
        max_chars = AgentSessionManager.SLIDING_WINDOW_SUMMARY_MAX
        char_count = 0
        for m in evicted:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            name = m.get("name", "")
            prefix = f"[{role}{'/' + name if name else ''}]"
            if content:
                snippet = content[:200] + ("..." if len(content) > 200 else "")
                line = f"{prefix} {snippet}"
                if char_count + len(line) > max_chars:
                    break
                summary_parts.append(line)
                char_count += len(line)

        summary_text = "\n".join(summary_parts)
        summary_msg = {
            "role": "user",
            "content": f"[早期对话摘要 — {len(evicted)} 条消息已压缩]\n{summary_text}",
        }
        ack_msg = {"role": "assistant", "content": "已了解早期上下文，继续对话。"}

        result = [*system_msgs, summary_msg, ack_msg, *kept]
        logger.debug(
            f"sliding_window: {len(non_system)} → {len(result)} 条消息 "
            f"(滑落 {len(evicted)} 条 → 摘要)"
        )
        return result

    @staticmethod
    def cleanup_orphaned_tool_calls(messages: list) -> list:
        """清理孤立的 tool_calls：移除那些没有对应 tool 响应的 tool_calls。

        OpenAI API 要求 assistant(tool_calls) 后面必须有 tool 消息覆盖所有 tool_call_id。
        此函数扫描整个消息列表，清理不满足此约束的 tool_calls。
        """
        result = list(messages)
        i = 0
        while i < len(result):
            msg = result[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                tc_ids = {tc.get("id", "") for tc in msg["tool_calls"] if tc.get("id")}
                if not tc_ids:
                    i += 1
                    continue
                n = 1
                while i + n < len(result) and result[i + n].get("role") == "tool":
                    n += 1
                tool_msgs = result[i + 1:i + n]
                responded_ids = {m.get("tool_call_id", "") for m in tool_msgs if m.get("tool_call_id")}
                missing = tc_ids - responded_ids
                if missing:
                    logger.warning(f"cleanup_orphaned_tool_calls: 发现 {len(missing)} 个孤立 tool_calls, 正在清理")
                    cleaned = {k: v for k, v in msg.items() if k != "tool_calls"}
                    if cleaned.get("content"):
                        result[i] = cleaned
                    else:
                        result.pop(i)
                        i -= 1
            i += 1
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
        tool_defs: list = None,
        session_id: str = "",
    ) -> list:
        """四层渐进式上下文压缩。

        Layer 0: sliding_window — 滑动窗口，始终裁剪到最近 N 条，零成本
        Layer 1: tool_collapse — 截断旧工具结果，零成本（仅 tool 角色）
        Layer 2: context_collapse — 折叠超长文本块，零成本（兜底 assistant/user 的长回复）
        Layer 3: LLM 压缩 — 超预算时调用模型生成结构化摘要，有成本
        """
        max_tokens = max_tokens or AgentSessionManager.MAX_CONTEXT_TOKENS
        token_count = AgentSessionManager.estimate_tokens(messages, tool_defs)

        # Layer 0: sliding_window — 始终裁剪到窗口大小
        non_system = [m for m in messages if m.get("role") != "system"]
        if len(non_system) > AgentSessionManager.SLIDING_WINDOW_SIZE:
            messages = AgentSessionManager.sliding_window(messages)

        # Layer 1: tool_collapse — 每轮无条件截断旧工具结果
        messages = AgentSessionManager.tool_collapse(messages)
        token_count = AgentSessionManager.estimate_tokens(messages, tool_defs)

        if token_count < max_tokens * 0.65:
            return messages

        # Layer 2: context_collapse — 折叠超长文本块（兜底 assistant/user 长内容）
        if token_count >= max_tokens * 0.65:
            messages = AgentSessionManager.context_collapse(messages)
            token_count = AgentSessionManager.estimate_tokens(messages, tool_defs)
            if token_count < max_tokens * 0.8:
                return messages

        # Layer 3: LLM 压缩 — 生成结构化摘要
        logger.info(f"上下文估计 {token_count} tokens，接近上限 {max_tokens}，启动 LLM 压缩...")

        system_msgs = [m for m in messages if m.get("role") == "system"]
        recent_msgs = messages[-8:]

        # 确保 recent_msgs 中 assistant(tool_calls) 后面有对应的 tool response
        tc_ids_in_recent = set()
        for m in recent_msgs:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    tc_id = tc.get("id", "")
                    if tc_id:
                        tc_ids_in_recent.add(tc_id)
        responded_ids = set()
        for m in recent_msgs:
            if m.get("role") == "tool" and m.get("tool_call_id"):
                responded_ids.add(m["tool_call_id"])
        missing_ids = tc_ids_in_recent - responded_ids
        if missing_ids:
            # 向前扩展 recent_msgs 直到包含所有 tool response
            for i in range(len(messages) - 9, -1, -1):
                m = messages[i]
                recent_msgs.insert(0, m)
                if m.get("role") == "tool" and m.get("tool_call_id") in missing_ids:
                    missing_ids.discard(m["tool_call_id"])
                if not missing_ids:
                    break

        # 清理 recent_msgs 中的孤儿 tool_response（对应 tool_calls 在 history 中）
        recent_tc_ids = set()
        for m in recent_msgs:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    tc_id = tc.get("id", "")
                    if tc_id:
                        recent_tc_ids.add(tc_id)
        recent_msgs = [m for m in recent_msgs if not (
            m.get("role") == "tool"
            and m.get("tool_call_id")
            and m["tool_call_id"] not in recent_tc_ids
        )]

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

            # 持久化压缩摘要，供重启后无损恢复（避免重新压缩/丢失上下文）
            if session_id and summary:
                try:
                    from storage.storage import get_storage
                    _st = get_storage()
                    if _st and hasattr(_st, "save_session_meta"):
                        _st.save_session_meta(session_id, summary)
                except Exception:  # noqa: BLE001
                    pass

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
        self.sessions: dict[str, AgentSession] = {}
        self._lock = asyncio.Lock()
        self.ttl_seconds = ttl_seconds or Config.SESSION_TTL_SECONDS
        self.max_sessions = max_sessions or Config.MAX_SESSIONS
        self._cleanup_task: asyncio.Task | None = None

    async def start_cleanup_task(self):
        """启动定期清理任务"""
        if self._cleanup_task:
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.debug("会话清理任务已启动")

    def stop_cleanup_task(self):
        """停止清理任务"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None
            logger.debug("会话清理任务已停止")

    async def _cleanup_loop(self):
        """定期清理过期会话"""
        while True:
            try:
                await asyncio.sleep(self.CLEANUP_INTERVAL)
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
        session_id: str | None = None,
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

    async def get_session(self, session_id: str) -> AgentSession | None:
        async with self._lock:
            session = self.sessions.get(session_id)
        if session:
            session.touch()
        return session

    async def remove_session(self, session_id: str):
        async with self._lock:
            if session_id in self.sessions:
                del self.sessions[session_id]
                logger.info(f"删除Session: {session_id}")

    def list_sessions(self) -> list[str]:
        return list(self.sessions.keys())

    def get_session_count(self) -> int:
        return len(self.sessions)

    def get_session_info(self) -> dict[str, Any]:
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
