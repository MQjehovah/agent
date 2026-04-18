import json
import logging
from typing import Dict, Any, List

from .categories import REFLECT_SKIP_TOOLS

logger = logging.getLogger("agent.learning.collector")

MAX_RESULT_PREVIEW = 200
MAX_ARGS_PREVIEW = 80
MAX_ENTRIES = 50


class ContextCollector:
    def __init__(self):
        self._entries: List[Dict[str, Any]] = []

    def reset(self):
        self._entries.clear()

    def record_tool_call(
        self, tool: str, args: Dict[str, Any], result: str, success: bool
    ):
        if tool in REFLECT_SKIP_TOOLS:
            return

        args_brief = json.dumps(args, ensure_ascii=False)[:MAX_ARGS_PREVIEW]
        result_brief = result[:MAX_RESULT_PREVIEW] if len(result) > MAX_RESULT_PREVIEW else result

        self._entries.append({
            "tool": tool,
            "args": args_brief,
            "result": result_brief,
            "success": success,
        })

        if len(self._entries) > MAX_ENTRIES:
            self._entries = self._entries[-MAX_ENTRIES:]

    def get_summary(self) -> str:
        if not self._entries:
            return ""

        lines = []
        success_count = sum(1 for e in self._entries if e["success"])
        fail_count = len(self._entries) - success_count

        lines.append(
            f"共 {len(self._entries)} 次工具调用"
            f"（成功 {success_count}，失败 {fail_count}）"
        )

        for i, entry in enumerate(self._entries, 1):
            status = "✓" if entry["success"] else "✗"
            lines.append(
                f"{i}. [{status}] {entry['tool']}({entry['args']}) → {entry['result']}"
            )

        return "\n".join(lines)

    @property
    def entry_count(self) -> int:
        return len(self._entries)