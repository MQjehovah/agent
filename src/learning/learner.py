import os
import re
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, TYPE_CHECKING

from .categories import (
    CORRECTION_KEYWORDS, REFLECT_PROMPT, REFLECT_SYSTEM_PROMPT,
    REFLECT_SKIP_TOOLS, MAX_SUMMARY_LENGTH, TOOL_RESULT_MAX,
    DAILY_EXTRACT_PROMPT, DAILY_EXTRACT_SYSTEM_PROMPT, CHUNK_SIZE,
)

if TYPE_CHECKING:
    from skills.skill import SkillManager
    from subagent_manager import SubagentManager

logger = logging.getLogger("agent.learning")


class Learner:
    """自学习模块 — 决定学什么、何时学，调用 MemoryManager 存储"""

    def __init__(self, memory_manager, llm_client=None, agent_id: str = ""):
        self.memory = memory_manager
        self.llm_client = llm_client
        self.agent_id = agent_id
        self._daily_task = None

        self._pattern_tracker = None
        self._auto_creator = None
        self._skill_manager: Optional["SkillManager"] = None
        self._subagent_manager: Optional["SubagentManager"] = None
        self._workspace = ""

    def set_llm_client(self, client):
        self.llm_client = client
        if self._pattern_tracker:
            self._pattern_tracker.set_llm_client(client)
        if self._auto_creator:
            self._auto_creator.set_llm_client(client)

    def init_auto_creation(
        self,
        workspace: str,
        skill_manager: "SkillManager" = None,
        subagent_manager: "SubagentManager" = None,
    ):
        """初始化模式追踪和自动创建（由 Agent._init_memory 调用）"""
        from .pattern_tracker import PatternTracker
        from .auto_creator import AutoCreator

        self._workspace = workspace
        self._skill_manager = skill_manager
        self._subagent_manager = subagent_manager

        memory_dir = os.path.join(workspace, "memory") if workspace else ""
        skills_dir = os.path.join(workspace, "skills") if workspace else ""
        agents_dir = os.path.join(workspace, "agents") if workspace else ""

        self._pattern_tracker = PatternTracker(memory_dir, llm_client=self.llm_client)
        self._auto_creator = AutoCreator(
            memory_dir=memory_dir,
            skills_dir=skills_dir,
            agents_dir=agents_dir,
            llm_client=self.llm_client,
            skill_manager=skill_manager,
            subagent_manager=subagent_manager,
        )

        logger.info(
            f"[自学习] 自动创建模块已初始化 "
            f"(skills={bool(skill_manager)}, subagents={bool(subagent_manager)})"
        )

    # ------------------------------------------------------------------ #
    #  每日提取定时任务
    # ------------------------------------------------------------------ #

    def start_daily_task(self):
        if self._daily_task:
            return
        self._daily_task = asyncio.create_task(self._daily_extract_loop())
        logger.info("[自学习] 每日记忆提取任务已启动")

    def stop_daily_task(self):
        if self._daily_task:
            self._daily_task.cancel()
            self._daily_task = None
            logger.info("[自学习] 每日记忆提取任务已停止")

    async def _daily_extract_loop(self):
        while True:
            try:
                now = datetime.now()
                tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=30, second=0, microsecond=0)
                seconds_until_midnight = (tomorrow - now).total_seconds()

                logger.debug(f"[自学习] 下次记忆提取: {tomorrow} ({seconds_until_midnight:.0f}秒后)")
                try:
                    await asyncio.sleep(seconds_until_midnight)
                except asyncio.CancelledError:
                    logger.info("[自学习] 记忆提取循环被取消")
                    return

                logger.info("[自学习] 开始每日记忆提取...")
                await self.daily_extract()
            except asyncio.CancelledError:
                logger.info("[自学习] 记忆提取循环被取消")
                return
            except Exception as e:
                logger.error(f"[自学习] 每日提取失败: {e}", exc_info=True)
                try:
                    await asyncio.sleep(300)
                except asyncio.CancelledError:
                    return

    # ------------------------------------------------------------------ #
    #  任务反思（任务完成后后台调用）
    # ------------------------------------------------------------------ #

    def check_user_correction(self, task: str) -> bool:
        if any(kw in task for kw in CORRECTION_KEYWORDS):
            self.memory.add_correction(
                context=task[:200],
                correction="之前的方法不被认可，需要换思路",
            )
            logger.info("[自学习] 检测到用户纠正信号")
            return True
        return False

    def record_failure(self, tool_name: str, args_summary: str, error: str):
        self.memory.add_failure_lesson(tool_name, args_summary[:80], error[:150])

    async def reflect_on_task(self, task: str, messages: list) -> int:
        summary = self._summarize_messages(messages)
        if not summary:
            logger.info("[自学习] 任务无有效摘要，跳过反思")
            return 0

        if not self.llm_client:
            logger.info("[自学习] 无 LLM 客户端，跳过反思")
            return 0

        logger.info(f"[自学习] 开始反思，摘要长度: {len(summary)} 字符")
        prompt = REFLECT_PROMPT.format(task=task[:300], summary=summary)

        try:
            text = await self._call_llm(REFLECT_SYSTEM_PROMPT, prompt)
            saved = self._parse_reflection(text)

            # 模式追踪：记录任务并检测是否需要自动创建
            if self._pattern_tracker and self._auto_creator:
                try:
                    pattern_info = await self._pattern_tracker.record_task(task, summary)
                    if pattern_info:
                        await self._auto_create(pattern_info)
                except Exception as e:
                    logger.warning(f"[自学习] 模式追踪/自动创建失败: {e}")

            return saved
        except Exception as e:
            logger.warning(f"[自学习] 反思失败: {e}")
            return 0

    async def _auto_create(self, pattern_info: dict):
        """执行自动创建并标记"""
        result_path = await self._auto_creator.create_from_pattern(pattern_info)
        if result_path:
            self._pattern_tracker.mark_created(pattern_info["pattern_key"])
            logger.info(
                f"[自学习] 已自动创建 "
                f"{pattern_info['category']}: {pattern_info['suggested_name']} "
                f"-> {result_path}"
            )
        else:
            logger.warning(
                f"[自学习] 自动创建失败: {pattern_info.get('pattern_key', 'unknown')}"
            )

    # ------------------------------------------------------------------ #
    #  每日提取（凌晨定时调用，由 MemoryManager 的定时任务触发）
    # ------------------------------------------------------------------ #

    async def daily_extract(self) -> None:
        """每日记忆提取入口：提取 → 保存 → 归档 → 整理"""
        # 确保 MemoryManager 有 LLM 客户端（归档整理需要）
        if self.llm_client and not self.memory._llm_client:
            self.memory.set_llm_client(self.llm_client)

        storage = self.memory._get_storage()
        if not storage:
            logger.debug("Storage未初始化，跳过每日提取")
            return

        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        agent_ids = storage.get_all_agent_ids()

        # 构建 agent_id -> workspace 映射（用于定位子代理记忆目录）
        subagent_workspaces = {}
        workspace_root = self.memory.workspace
        for agent_id in agent_ids:
            if agent_id == self.memory.agent_id:
                continue
            # 子代理的 workspace 在 agents/<agent_id> 下
            sub_ws = os.path.join(workspace_root, "agents", agent_id)
            if os.path.isdir(sub_ws):
                subagent_workspaces[agent_id] = sub_ws

        for agent_id in agent_ids:
            messages = storage.get_messages_by_date(date_str, agent_id=agent_id)
            if not messages:
                logger.debug(f"[{agent_id}] 无 {date_str} 的对话记录")
                continue

            session_text = self._format_daily_messages(messages)
            if not session_text:
                continue

            extracted = await self._extract_daily_text(session_text, agent_id)
            if extracted:
                header = f"# 每日记忆 - {date_str}\n\n"
                ws = subagent_workspaces.get(agent_id)
                self.memory.save_daily(agent_id, date_str, header + extracted, workspace=ws)
                logger.info(f"[每日提取] Agent [{agent_id}] 提取完成")
            else:
                logger.debug(f"[每日提取] Agent [{agent_id}] 无关键信息")

        # 归档与整理
        await self.memory.archive_to_long_term(subagent_workspaces=subagent_workspaces)
        await self.memory.consolidate_long_term(subagent_workspaces=subagent_workspaces)
        await self.memory.prune_long_term(subagent_workspaces=subagent_workspaces)

    async def _extract_daily_text(self, session_text: str, agent_id: str) -> str:
        """用 LLM 从对话文本提取关键信息，失败则回退到简单截断"""
        if not self.llm_client:
            return self._simple_extract(session_text)

        try:
            if len(session_text) <= CHUNK_SIZE:
                return await self._extract_chunk(session_text, agent_id)

            # 长文本分片
            chunks = self._split_text(session_text)
            logger.info(f"对话过长 ({len(session_text)} 字符)，分为 {len(chunks)} 片提取")

            results = []
            for chunk in chunks:
                extracted = await self._extract_chunk(chunk, agent_id)
                if extracted:
                    results.append(extracted)

            return "\n\n".join(results) if results else ""
        except Exception as e:
            logger.warning(f"每日 LLM 提取失败，回退到简单模式: {e}")
            return self._simple_extract(session_text)

    async def _extract_chunk(self, chunk: str, agent_id: str) -> str:
        prompt = DAILY_EXTRACT_PROMPT.format(agent_id=agent_id, chunk=chunk)
        try:
            result = await self._call_llm(DAILY_EXTRACT_SYSTEM_PROMPT, prompt)
            if "无关键信息" in result:
                return ""
            return result
        except Exception as e:
            logger.warning(f"每日记忆提取失败: {e}")
            return ""

    def _simple_extract(self, session_text: str) -> str:
        """无 LLM 时的简单回退"""
        lines = session_text.split("\n")
        trimmed = [line[:200] + "..." if len(line) > 200 else line for line in lines]
        return f"## 会话摘要\n\n" + "\n".join(trimmed[:50])

    def _split_text(self, text: str) -> list:
        lines = text.split("\n")
        chunks = []
        current = []
        current_len = 0
        for line in lines:
            line_len = len(line) + 1
            if current_len + line_len > CHUNK_SIZE and current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += line_len
        if current:
            chunks.append("\n".join(current))
        return chunks

    def _format_daily_messages(self, messages: list) -> str:
        """将存储的消息格式化为提取用文本"""
        session_lines = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                session_lines.append(f"用户: {content[:500]}")
            elif role == "assistant":
                session_lines.append(f"助手: {content[:500]}")
            elif role == "tool":
                session_lines.append(f"工具结果: {content[:200]}")

        return "\n".join(session_lines) if session_lines else ""

    # ------------------------------------------------------------------ #
    #  内部方法
    # ------------------------------------------------------------------ #

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        response = await self.llm_client.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=None,
            stream=False,
            use_cache=False,
        )
        return (response.choices[0].message.content or "").strip()

    def _summarize_messages(self, messages: list) -> str:
        """从 session.messages 生成执行摘要"""
        lines = []
        total_len = 0
        skipped = {"system": 0, "tool_skip": 0, "empty": 0}

        for msg in messages:
            if isinstance(msg, dict):
                role = msg.get("role", "")
                content = msg.get("content", "")
                name = msg.get("name", "")
                tool_calls = msg.get("tool_calls")
            else:
                role = getattr(msg, "role", "")
                content = getattr(msg, "content", "") or ""
                name = getattr(msg, "name", "") or ""
                tool_calls = getattr(msg, "tool_calls", None)

            if role == "system":
                skipped["system"] += 1
                continue

            if role == "user":
                line = f"用户: {content[:200]}"
            elif role == "assistant":
                if tool_calls:
                    tc_lines = []
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            func = tc.get("function", {})
                            tname = func.get("name") or func.get("name", "")
                            targs = func.get("arguments", "")
                        else:
                            func = getattr(tc, "function", None)
                            tname = getattr(func, "name", "") if func else ""
                            targs = getattr(func, "arguments", "") if func else ""
                        if isinstance(targs, str) and len(targs) > 100:
                            targs = targs[:100] + "..."
                        tc_lines.append(f"{tname}({targs})")
                    line = f"调用: {', '.join(tc_lines)}"
                else:
                    line = f"助手: {(content or '')[:200]}"
            elif role == "tool":
                tool_name = name or "tool"
                if tool_name in REFLECT_SKIP_TOOLS:
                    skipped["tool_skip"] += 1
                    continue
                line = f"[{tool_name}] {(content or '')[:TOOL_RESULT_MAX]}"
            else:
                continue

            if not line or len(line) <= 3:
                skipped["empty"] += 1
                continue

            if total_len + len(line) > MAX_SUMMARY_LENGTH:
                lines.append("... (后续内容省略)")
                break

            lines.append(line)
            total_len += len(line)

        logger.debug(f"[自学习] 消息摘要: {len(lines)} 行, 跳过: {skipped}")
        return "\n".join(lines) if lines else ""

    def _parse_reflection(self, text: str) -> int:
        saved = 0
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue

            save_match = re.match(
                r'(?:SAVE|保存)\s*[:：]\s*(.+)', line, re.IGNORECASE
            )
            if save_match:
                knowledge = save_match.group(1).strip()
                if knowledge:
                    self.memory.add_reflection(knowledge)
                    self.memory.share_knowledge(self.agent_id or "主代理", knowledge)
                    logger.info(f"[自学习] 反思提取: {knowledge[:80]}")
                    saved += 1
        return saved