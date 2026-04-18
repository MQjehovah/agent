import re
import logging
from typing import Optional

from .categories import CORRECTION_KEYWORDS, REFLECT_PROMPT, REFLECT_SYSTEM_PROMPT
from .writer import MemoryWriter
from .collector import ContextCollector

logger = logging.getLogger("agent.learning")


class Learner:
    def __init__(
        self,
        memory_dir: str,
        agent_id: str = "",
        llm_client=None,
        shared_knowledge_file: Optional[str] = None,
    ):
        self.agent_id = agent_id
        self.llm_client = llm_client
        self.writer = MemoryWriter(memory_dir, shared_knowledge_file)

    def set_llm_client(self, client):
        self.llm_client = client

    def check_user_correction(self, task: str) -> bool:
        if any(kw in task for kw in CORRECTION_KEYWORDS):
            self.writer.write_correction(
                context=task[:200],
                correction="之前的方法不被认可，需要换思路",
            )
            logger.info("[自学习] 检测到用户纠正信号")
            return True
        return False

    def record_failure(self, tool_name: str, args_summary: str, error: str):
        self.writer.write_failure(tool_name, args_summary[:80], error[:150])

    async def reflect_on_task(self, task: str, collector: ContextCollector) -> int:
        summary = collector.get_summary()
        if not summary:
            return 0

        if not self.llm_client:
            return 0

        prompt = REFLECT_PROMPT.format(task=task[:300], summary=summary)

        try:
            response = await self.llm_client.chat(
                messages=[
                    {"role": "system", "content": REFLECT_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                stream=False,
                use_cache=False,
            )
            text = (response.choices[0].message.content or "").strip()
            return self._parse_reflection(text)
        except Exception as e:
            logger.warning(f"[自学习] 反思失败: {e}")
            return 0

    def _parse_reflection(self, text: str) -> int:
        saved = 0
        for line in text.split("\n"):
            line = line.strip()
            save_match = re.match(
                r'(?:SAVE|保存)\s*[:：]\s*(.+)', line, re.IGNORECASE
            )
            if save_match:
                knowledge = save_match.group(1).strip()
                if knowledge:
                    self.writer.write_reflection(knowledge, self.agent_id or "主代理")
                    logger.info(f"[自学习] 反思提取: {knowledge[:80]}")
                    saved += 1
            elif line.strip() and not line.upper().startswith("SKIP"):
                if len(line) > 5 and not line.startswith(("-", "*", "#", "1.", "2.")):
                    self.writer.write_reflection(line.strip(), self.agent_id or "主代理")
                    logger.info(f"[自学习] 反思提取: {line.strip()[:80]}")
                    saved += 1
        return saved