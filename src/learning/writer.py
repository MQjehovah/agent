import os
import logging
from datetime import datetime
from typing import Optional

from .categories import MemoryCategory

logger = logging.getLogger("agent.learning.writer")


class MemoryWriter:
    def __init__(self, memory_dir: str, shared_knowledge_file: Optional[str] = None):
        self.memory_dir = memory_dir
        self.long_term_file = os.path.join(memory_dir, "memory.md")
        self.shared_knowledge_file = shared_knowledge_file or os.path.join(
            memory_dir, "shared_knowledge.md"
        )
        self._ensure_dirs()

    def _ensure_dirs(self):
        os.makedirs(self.memory_dir, exist_ok=True)
        daily_dir = os.path.join(self.memory_dir, "daily")
        os.makedirs(daily_dir, exist_ok=True)

    def write(self, category: MemoryCategory, content: str) -> bool:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        return self._append_to_memory(category.value, content, timestamp)

    def write_reflection(self, knowledge: str, agent_id: str = "") -> bool:
        ok = self.write(MemoryCategory.REFLECTION, knowledge)
        if ok and agent_id:
            self.share_knowledge(agent_id, knowledge)
        return ok

    def write_correction(self, context: str, correction: str) -> bool:
        formatted = f"场景: {context} | 纠正: {correction}"
        return self.write(MemoryCategory.CORRECTION, formatted)

    def write_failure(self, tool_name: str, args_summary: str, error: str) -> bool:
        formatted = f"{tool_name}({args_summary}) 失败: {error}"
        return self.write(MemoryCategory.FAILURE_LESSON, formatted)

    def share_knowledge(self, from_agent: str, knowledge: str) -> bool:
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            entry = f"- [{timestamp}][{from_agent}] {knowledge}\n"
            self._ensure_dirs()
            if not os.path.exists(self.shared_knowledge_file):
                with open(self.shared_knowledge_file, "w", encoding="utf-8") as f:
                    f.write("# 共享知识库\n\n跨代理共享的经验和知识。\n\n")
            with open(self.shared_knowledge_file, "a", encoding="utf-8") as f:
                f.write(entry)
            logger.debug(f"Shared knowledge from [{from_agent}]: {knowledge[:80]}")
            return True
        except Exception as e:
            logger.error(f"Failed to write shared knowledge: {e}")
            return False

    def _append_to_memory(self, category: str, content: str, timestamp: str) -> bool:
        try:
            self._ensure_dirs()

            if not os.path.exists(self.long_term_file):
                with open(self.long_term_file, "w", encoding="utf-8") as f:
                    f.write(f"# 长期记忆\n\n## {category}\n\n- [{timestamp}] {content}\n")
                logger.info(f"Memory created: [{category}] {content[:80]}")
                return True

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

            new_content = "".join(lines)
            with open(self.long_term_file, "w", encoding="utf-8") as f:
                f.write(new_content)

            logger.info(f"Memory saved: [{category}] {content[:80]}")
            return True
        except Exception as e:
            logger.error(f"Memory write error: [{category}] {e}")
            return False