import os
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("agent.memory")


class MemoryManager:
    """记忆存储管理器 — 只负责读写、归档，不负责提取决策"""

    def __init__(self, workspace: str, agent_id: str = ""):
        self.workspace = workspace
        self.agent_id = agent_id
        self.memory_dir = os.path.join(workspace, "memory")
        self.daily_dir = os.path.join(self.memory_dir, "daily")
        self.long_term_file = os.path.join(self.memory_dir, "memory.md")
        self.shared_knowledge_file = os.path.join(self.memory_dir, "shared_knowledge.md")
        self._llm_client = None

        self._ensure_dirs()

    def set_llm_client(self, client):
        self._llm_client = client

    def _get_storage(self):
        from storage import get_storage
        return get_storage()

    def _ensure_dirs(self):
        os.makedirs(self.memory_dir, exist_ok=True)
        os.makedirs(self.daily_dir, exist_ok=True)
        if not os.path.exists(self.shared_knowledge_file):
            with open(self.shared_knowledge_file, "w", encoding="utf-8") as f:
                f.write("# 共享知识库\n\n跨代理共享的经验和知识。\n\n")

    # ------------------------------------------------------------------ #
    #  归档与整理 — 纯文件操作，不涉及 LLM
    # ------------------------------------------------------------------ #

    async def archive_to_long_term(self, subagent_workspaces: dict = None):
        """归档主 agent + 子 agent 的每日记忆到长期记忆。
        subagent_workspaces: {agent_id: workspace_path} 映射"""
        from .archiver import MemoryArchiver

        # 主 agent
        archiver = MemoryArchiver(self.memory_dir)
        archiver.cleanup_old_files(retention_days=7)
        archiver.archive_daily_to_long_term(days_threshold=1)

        # 子 agent — 优先从 workspace 映射找，兼容旧的 agents/ 子目录
        if subagent_workspaces:
            for agent_id, ws in subagent_workspaces.items():
                sub_memory_dir = os.path.join(ws, "memory")
                if os.path.isdir(sub_memory_dir):
                    sub_archiver = MemoryArchiver(sub_memory_dir)
                    sub_archiver.cleanup_old_files(retention_days=7)
                    sub_archiver.archive_daily_to_long_term(days_threshold=1)
        else:
            agents_dir = os.path.join(self.memory_dir, "agents")
            if os.path.exists(agents_dir):
                for agent_name in os.listdir(agents_dir):
                    agent_memory_dir = os.path.join(agents_dir, agent_name)
                    if os.path.isdir(agent_memory_dir):
                        sub_archiver = MemoryArchiver(agent_memory_dir)
                        sub_archiver.cleanup_old_files(retention_days=7)
                        sub_archiver.archive_daily_to_long_term(days_threshold=1)

        logger.info("每日记忆归档完成")

    async def consolidate_long_term(self, file_path: str = None, subagent_workspaces: dict = None):
        """整理长期记忆：合并重复条目"""
        if not self._llm_client:
            return

        # 主 agent
        target = file_path or self.long_term_file
        await self._consolidate_one(target)

        # 子 agent
        if subagent_workspaces:
            for agent_id, ws in subagent_workspaces.items():
                sub_long_term = os.path.join(ws, "memory", "memory.md")
                if os.path.isfile(sub_long_term):
                    await self._consolidate_one(sub_long_term)

    async def _consolidate_one(self, target: str):
        """整理单个长期记忆文件"""
        if not os.path.exists(target):
            return

        with open(target, "r", encoding="utf-8") as f:
            content = f.read()

        if len(content) < 100:
            return

        backup_path = target + ".bak"
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(content)

        prompt = (
            "请整理以下长期记忆，要求：\n"
            "1. 合并重复或高度相似的信息\n"
            "2. 删除明显已过时的信息（如已完成的待办）\n"
            "3. 保持分类结构，无内容的分类省略\n"
            "4. 每条信息保持简洁，用一句话概括\n"
            "5. 不要删除任何有效的经验、偏好或关键信息\n\n"
            f"原始记忆：\n{content}\n\n"
            "只输出整理后的结果，保持 markdown 格式。"
        )

        try:
            response = await self._llm_client.chat(
                messages=[
                    {"role": "system", "content": "你是记忆整理助手。保持信息完整，去除冗余。不要删除任何有用的信息。"},
                    {"role": "user", "content": prompt}
                ],
                tools=None, stream=False, use_cache=False
            )
            result = response.choices[0].message.content or ""
            if result.strip():
                with open(target, "w", encoding="utf-8") as f:
                    f.write(result)
                    f.flush()
                    os.fsync(f.fileno())
                logger.info(f"长期记忆整理完成: {target}")
        except Exception as e:
            if os.path.exists(backup_path):
                with open(backup_path, "r", encoding="utf-8") as bf:
                    original = bf.read()
                with open(target, "w", encoding="utf-8") as f:
                    f.write(original)
            logger.warning(f"长期记忆整理失败（已恢复备份）: {e}")
        finally:
            if os.path.exists(backup_path):
                try:
                    os.remove(backup_path)
                except Exception:
                    pass

    async def prune_long_term(self, file_path: str = None, subagent_workspaces: dict = None):
        """用 LLM 评估并删除低价值记忆条目"""
        if not self._llm_client:
            return

        from .archiver import MemoryArchiver

        # 主 agent
        target = file_path or self.long_term_file
        archiver = MemoryArchiver(self.memory_dir)
        await archiver.score_and_prune(target, self._llm_client)

        # 子 agent
        if subagent_workspaces:
            for agent_id, ws in subagent_workspaces.items():
                sub_memory_dir = os.path.join(ws, "memory")
                sub_long_term = os.path.join(sub_memory_dir, "memory.md")
                if os.path.isfile(sub_long_term):
                    sub_archiver = MemoryArchiver(sub_memory_dir)
                    await sub_archiver.score_and_prune(sub_long_term, self._llm_client)

    # ------------------------------------------------------------------ #
    #  写入方法
    # ------------------------------------------------------------------ #

    def _append_to_memory(self, category: str, content: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            self._ensure_dirs()
            if not os.path.exists(self.long_term_file):
                with open(self.long_term_file, "w", encoding="utf-8") as f:
                    f.write(f"# 长期记忆\n\n## {category}\n\n- [{timestamp}] {content}\n")
                logger.info(f"Memory created: [{category}] {content[:80]}")
                return
            with open(self.long_term_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            category_header = f"## {category}"
            category_idx = -1
            for i, line in enumerate(lines):
                if line.strip() == category_header:
                    category_idx = i
                    break
            if category_idx == -1:
                lines.append(f"\n## {category}\n\n- [{timestamp}] {content}\n")
            else:
                insert_idx = category_idx + 1
                while insert_idx < len(lines) and not lines[insert_idx].strip().startswith("## "):
                    insert_idx += 1
                lines.insert(insert_idx, f"- [{timestamp}] {content}\n")
            with open(self.long_term_file, "w", encoding="utf-8") as f:
                f.writelines(lines)
            logger.info(f"Memory saved: [{category}] {content[:80]}")
        except Exception as e:
            logger.error(f"Memory write error: [{category}] {e}")

    def add_preference(self, preference: str):
        self._append_to_memory("用户偏好", preference)

    def add_key_info(self, info: str):
        self._append_to_memory("关键信息", info)

    def add_todo(self, todo: str):
        self._append_to_memory("待办事项", todo)

    def add_failure_lesson(self, tool_name: str, args_summary: str, error: str):
        self._append_to_memory("避坑经验", f"{tool_name}({args_summary}) 失败: {error}")

    def add_correction(self, context: str, correction: str):
        self._append_to_memory("用户纠正", f"场景: {context} | 纠正: {correction}")

    def add_reflection(self, knowledge: str):
        self._append_to_memory("自学习", knowledge)

    def share_knowledge(self, from_agent: str, knowledge: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"- [{timestamp}][{from_agent}] {knowledge}\n"
        with open(self.shared_knowledge_file, "a", encoding="utf-8") as f:
            f.write(entry)
        logger.debug(f"Shared knowledge from [{from_agent}]: {knowledge[:80]}")

    def save_daily(self, agent_id: str, date_str: str, content: str, workspace: str = None):
        """保存每日记忆文件。workspace 指定目标 agent 的 workspace 目录。"""
        if workspace:
            daily_dir = os.path.join(workspace, "memory", "daily")
        elif agent_id == self.agent_id:
            daily_dir = self.daily_dir
        else:
            # 其他 agent 但未指定 workspace，按旧逻辑兼容
            daily_dir = os.path.join(self.memory_dir, "agents", agent_id, "daily")
        os.makedirs(daily_dir, exist_ok=True)
        daily_file = os.path.join(daily_dir, f"{date_str}.md")

        with open(daily_file, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"Daily memory saved: {daily_file}")

    # ------------------------------------------------------------------ #
    #  读取方法
    # ------------------------------------------------------------------ #

    def load_shared_knowledge(self) -> str:
        if not os.path.exists(self.shared_knowledge_file):
            return ""
        with open(self.shared_knowledge_file, "r", encoding="utf-8") as f:
            content = f.read()
        if content.strip() == "# 共享知识库\n\n跨代理共享的经验和知识。\n":
            return ""
        return content

    def load_memory(self, task: str = "") -> str:
        parts = []

        long_term = self._load_long_term(task)
        if long_term:
            parts.append(f"【长期记忆】\n{long_term}")

        daily = self._load_recent_daily(days=3)
        if daily:
            parts.append(f"【近期记忆】\n{daily}")

        shared = self.load_shared_knowledge()
        if shared:
            parts.append(f"【共享知识】\n{shared}")

        if parts:
            return "\n\n".join(parts)
        return ""

    def _load_long_term(self, task: str) -> str:
        if not os.path.exists(self.long_term_file):
            return ""

        with open(self.long_term_file, "r", encoding="utf-8") as f:
            content = f.read()

        if not task:
            return content

        keywords = task.lower().split()
        lines = content.split("\n")
        relevant_lines = []
        current_section = []
        in_relevant_section = False

        for line in lines:
            if line.startswith("## "):
                in_relevant_section = any(kw in line.lower() for kw in keywords)
                if in_relevant_section:
                    current_section = [line]
            elif in_relevant_section:
                current_section.append(line)
                if line.startswith("## ") or line.startswith("# "):
                    relevant_lines.extend(current_section[:-1])
                    current_section = [line]
                    in_relevant_section = any(kw in line.lower() for kw in keywords)

        if current_section and in_relevant_section:
            relevant_lines.extend(current_section)

        return "\n".join(relevant_lines) if relevant_lines else content[:500]

    def _load_recent_daily(self, days: int = 3) -> str:
        contents = []
        for i in range(days):
            date = datetime.now() - timedelta(days=i)
            filename = date.strftime("%Y-%m-%d.md")
            filepath = os.path.join(self.daily_dir, filename)

            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                contents.append(f"### {filename}\n{content}")

        return "\n\n".join(contents)

    def list_daily_files(self) -> list:
        if not os.path.exists(self.daily_dir):
            return []
        return [f for f in os.listdir(self.daily_dir) if f.endswith(".md")]