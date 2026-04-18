import re
import logging

from .categories import (
    CORRECTION_KEYWORDS, REFLECT_PROMPT, REFLECT_SYSTEM_PROMPT,
    REFLECT_SKIP_TOOLS, MAX_SUMMARY_LENGTH, TOOL_RESULT_MAX,
)

logger = logging.getLogger("agent.learning")


class Learner:
    def __init__(self, memory_manager, llm_client=None, agent_id: str = ""):
        self.memory = memory_manager
        self.llm_client = llm_client
        self.agent_id = agent_id

    def set_llm_client(self, client):
        self.llm_client = client

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

    def _summarize_messages(self, messages: list) -> str:
        """从 session.messages 生成执行摘要，只保留有价值的信息"""
        lines = []
        total_len = 0

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            name = msg.get("name", "")

            if role == "system":
                continue

            if role == "user":
                line = f"用户: {(content or '')[:200]}"
            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    for tc in tool_calls:
                        func = tc.get("function", {})
                        tname = func.get("name", "")
                        targs = func.get("arguments", "")
                        if isinstance(targs, str) and len(targs) > 100:
                            targs = targs[:100] + "..."
                        line = f"调用: {tname}({targs})"
                else:
                    line = f"助手: {(content or '')[:200]}"
            elif role == "tool":
                tool_name = name or "tool"
                if tool_name in REFLECT_SKIP_TOOLS:
                    continue
                line = f"[{tool_name}] {(content or '')[:TOOL_RESULT_MAX]}"
            else:
                continue

            if total_len + len(line) > MAX_SUMMARY_LENGTH:
                lines.append("... (后续内容省略)")
                break

            lines.append(line)
            total_len += len(line)

        return "\n".join(lines) if lines else ""

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
                    self.memory.add_reflection(knowledge)
                    self.memory.share_knowledge(self.agent_id or "主代理", knowledge)
                    logger.info(f"[自学习] 反思提取: {knowledge[:80]}")
                    saved += 1
            elif line.strip() and not line.upper().startswith("SKIP"):
                if len(line) > 5 and not line.startswith(("-", "*", "#", "1.", "2.")):
                    self.memory.add_reflection(line.strip())
                    self.memory.share_knowledge(self.agent_id or "主代理", line.strip())
                    logger.info(f"[自学习] 反思提取: {line.strip()[:80]}")
                    saved += 1
        return saved