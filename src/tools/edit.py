import os
import json
import logging
from difflib import SequenceMatcher
from typing import Dict, Any

from . import BuiltinTool

logger = logging.getLogger("agent.tools")


def _normalize_for_match(text: str) -> str:
    """标准化文本用于匹配：去掉每行尾部空白，统一换行符为 \\n"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines)


class EditTool(BuiltinTool):
    """行级文件编辑工具 — 精确替换文件中的指定内容"""

    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return """对文件进行精确的行级编辑。通过指定旧内容和新内容来替换文件中的特定部分。
支持在同一文件上执行多次替换操作。

使用场景：
- 修改代码中的某个函数实现
- 更新配置文件中的某个值
- 修复文件中的特定错误
- 重命名变量或函数

注意：old_text 必须是文件中唯一存在的文本，否则操作会失败。请提供足够的上下文使其唯一。
工具会自动处理尾部空白和换行符差异，你只需提供文件中出现的实际代码内容即可。"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要编辑的文件路径"
                },
                "old_text": {
                    "type": "string",
                    "description": "要被替换的原文本（必须精确匹配）"
                },
                "new_text": {
                    "type": "string",
                    "description": "替换后的新文本"
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换所有匹配项（默认仅替换第一个匹配）",
                    "default": False
                }
            },
            "required": ["path", "old_text", "new_text"]
        }

    async def execute(self, **kwargs) -> str:
        path = kwargs.get("path", "")
        old_text = kwargs.get("old_text", "")
        new_text = kwargs.get("new_text", "")
        replace_all = kwargs.get("replace_all", False)

        if not path:
            return json.dumps({"success": False, "error": "文件路径不能为空"}, ensure_ascii=False)
        if not old_text:
            return json.dumps({"success": False, "error": "原文本不能为空"}, ensure_ascii=False)
        if old_text == new_text:
            return json.dumps({"success": False, "error": "原文本和新文本相同"}, ensure_ascii=False)

        if not os.path.exists(path):
            return json.dumps({"success": False, "error": f"文件不存在: {path}"}, ensure_ascii=False)
        if os.path.isdir(path):
            return json.dumps({"success": False, "error": f"路径是目录: {path}"}, ensure_ascii=False)

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            return json.dumps({"success": False, "error": f"读取文件失败: {e}"}, ensure_ascii=False)

        normalized_content = _normalize_for_match(content)
        normalized_old = _normalize_for_match(old_text)

        occurrences = normalized_content.count(normalized_old)
        if occurrences == 0:
            hint = self._build_mismatch_hint(normalized_content, normalized_old)
            return json.dumps({
                "success": False,
                "error": "未找到匹配的文本",
                "hint": hint
            }, ensure_ascii=False)
        if occurrences > 1 and not replace_all:
            return json.dumps({
                "success": False,
                "error": f"找到 {occurrences} 处匹配，请提供更多上下文使匹配唯一，或设置 replace_all=true"
            }, ensure_ascii=False)

        normalized_new = _normalize_for_match(new_text)
        if replace_all:
            new_normalized = normalized_content.replace(normalized_old, normalized_new)
        else:
            new_normalized = normalized_content.replace(normalized_old, normalized_new, 1)

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_normalized)
        except Exception as e:
            return json.dumps({"success": False, "error": f"写入文件失败: {e}"}, ensure_ascii=False)

        return json.dumps({
            "success": True,
            "path": path,
            "replacements": occurrences if replace_all else 1,
            "message": f"成功替换 {occurrences if replace_all else 1} 处匹配"
        }, ensure_ascii=False)

    @staticmethod
    def _build_mismatch_hint(content: str, old_text: str) -> str:
        """当匹配失败时，找到最相似的位置作为提示"""
        if not old_text.strip():
            return ""
        first_line = old_text.strip().split("\n")[0].strip()
        if not first_line:
            return ""

        best_ratio = 0.0
        best_line = 0
        best_text = ""
        content_lines = content.split("\n")

        import re as _re
        clean_key = _re.sub(r'[^\w]', ' ', first_line[:50])
        keywords = [w for w in clean_key.split() if len(w) > 2]

        for i, line in enumerate(content_lines):
            if not keywords:
                ratio = SequenceMatcher(None, line.strip(), first_line).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_line = i + 1
                    start = max(0, i - 1)
                    end = min(len(content_lines), i + 3)
                    best_text = "\n".join(
                        f"  {start + j + 1:6d}\t{content_lines[start + j]}"
                        for j in range(end - start)
                    )
            else:
                match_count = sum(1 for kw in keywords if kw in line)
                if match_count > 0:
                    ratio = SequenceMatcher(None, line.strip(), first_line).ratio()
                    score = ratio + 0.2 * match_count
                    if score > best_ratio:
                        best_ratio = score
                        best_line = i + 1
                        start = max(0, i - 1)
                        end = min(len(content_lines), i + 3)
                        best_text = "\n".join(
                            f"  {start + j + 1:6d}\t{content_lines[start + j]}"
                            for j in range(end - start)
                        )

        if best_ratio > 0.3 and best_text:
            return (
                f"最相似的内容在第 {best_line} 行附近：\n{best_text}\n"
                f"请对比 old_text 与文件实际内容，注意空白、缩进、引号等差异。"
            )
        return ""
