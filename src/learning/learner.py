import os
import re
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, TYPE_CHECKING

from .categories import (
    CORRECTION_KEYWORDS, REFLECT_PROMPT, REFLECT_SYSTEM_PROMPT,
    REFLECT_SKIP_TOOLS, MAX_SUMMARY_LENGTH, TOOL_RESULT_MAX,
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
        self._curator = None

    def set_curator(self, curator):
        self._curator = curator

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

    def check_user_correction(self, task: str, user_id: str = "") -> bool:
        if any(kw in task for kw in CORRECTION_KEYWORDS):
            self.memory.add_correction(
                user_id,
                task[:200],
                "之前的方法不被认可，需要换思路",
            )
            logger.info("[自学习] 检测到用户纠正信号")
            return True
        return False

    def record_failure(self, tool_name: str, args_summary: str, error: str, user_id: str = ""):
        self.memory.add_failure_lesson(user_id, tool_name, args_summary[:80], error[:150])

    async def reflect_on_task(self, task: str, messages: list, user_id: str = "") -> int:
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
            saved = self._parse_reflection(text, user_id)

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
    #  每日提取（凌晨定时调用，触发 curator 提炼通用知识）
    # ------------------------------------------------------------------ #

    async def daily_extract(self) -> None:
        """每日：触发 curator 提炼通用知识（替代旧文件归档）"""
        if self._curator:
            try:
                await self._curator.curate_once()
            except Exception as e:
                logger.warning(f"[自学习] curator 执行失败: {e}")

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

    def _parse_reflection(self, text: str, user_id: str = "") -> int:
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
                    self.memory.add_reflection(user_id, knowledge)
                    logger.info(f"[自学习] 反思提取: {knowledge[:80]}")
                    saved += 1
        return saved
