import json
import sys
import asyncio
import logging
from typing import Dict, Any, Callable, Optional

from . import BuiltinTool

logger = logging.getLogger("agent.tools")


class AskUserTool(BuiltinTool):
    """用户交互工具 — 在执行过程中向用户提问或请求确认"""

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def description(self) -> str:
        return """在执行过程中向用户提问，获取用户的输入或确认。会暂停 Agent 执行，等待用户回复后继续。

使用场景：
- 执行危险操作前请求确认
- 需要用户提供额外信息
- 提供多个选项让用户选择
- 展示中间结果请用户决策"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要向用户提问的问题"
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选的选项列表，用户可以从中选择"
                },
                "default": {
                    "type": "string",
                    "description": "默认值（用户直接回车时使用）"
                }
            },
            "required": ["question"]
        }

    def __init__(self, input_handler: Optional[Callable] = None):
        self._input_handler = input_handler

    def set_input_handler(self, handler: Callable):
        """设置外部输入处理函数，用于非控制台交互模式（如 DingTalk、Webhook）"""
        self._input_handler = handler

    async def execute(self, **kwargs) -> str:
        question = kwargs.get("question", "")
        options = kwargs.get("options", [])
        default = kwargs.get("default", "")

        if not question:
            return json.dumps({"success": False, "error": "问题不能为空"}, ensure_ascii=False)

        if not self._input_handler and not sys.stdin.isatty():
            logger.warning("ask_user: 非交互式环境，无法获取用户输入，使用默认值")
            answer = default or ""
            return json.dumps({
                "success": True,
                "question": question,
                "answer": answer,
                "auto": True,
                "note": "非交互式环境，自动使用默认值"
            }, ensure_ascii=False)

        if self._input_handler:
            try:
                answer = await self._input_handler(question, options, default)
            except Exception as e:
                logger.error(f"ask_user: 输入处理失败: {e}")
                answer = default or ""
                return json.dumps({
                    "success": True,
                    "question": question,
                    "answer": answer,
                    "auto": True,
                    "note": f"输入处理异常，使用默认值: {e}"
                }, ensure_ascii=False)
        else:
            try:
                answer = await asyncio.get_event_loop().run_in_executor(
                    None, self._console_input, question, options, default
                )
            except (EOFError, KeyboardInterrupt, OSError) as e:
                logger.warning(f"ask_user: 控制台输入失败({type(e).__name__})，使用默认值")
                answer = default or ""
                return json.dumps({
                    "success": True,
                    "question": question,
                    "answer": answer,
                    "auto": True,
                    "note": f"控制台输入不可用({type(e).__name__})，自动使用默认值"
                }, ensure_ascii=False)

        return json.dumps({
            "success": True,
            "question": question,
            "answer": answer
        }, ensure_ascii=False)

    @staticmethod
    def _console_input(question: str, options: list, default: str) -> str:
        if options:
            print(f"\n{question}")
            for i, opt in enumerate(options, 1):
                print(f"  {i}. {opt}")
            prompt_str = f"请选择 (1-{len(options)}"
            if default:
                prompt_str += f", 默认: {default}"
            prompt_str += "): "
            raw = input(prompt_str).strip()
            if raw == "" and default:
                return default
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1]
            return raw
        else:
            prompt_str = question
            if default:
                prompt_str += f" (默认: {default})"
            prompt_str += ": "
            raw = input(prompt_str).strip()
            return raw if raw else default