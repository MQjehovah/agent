import os
import json
import logging
from typing import Dict, Any

from . import BuiltinTool

logger = logging.getLogger("agent.tools")


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

注意：old_text 必须是文件中唯一存在的文本，否则操作会失败。请提供足够的上下文使其唯一。"""

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
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return json.dumps({"success": False, "error": f"读取文件失败: {e}"}, ensure_ascii=False)

        occurrences = content.count(old_text)
        if occurrences == 0:
            return json.dumps({"success": False, "error": "未找到匹配的文本"}, ensure_ascii=False)
        if occurrences > 1 and not replace_all:
            return json.dumps({
                "success": False,
                "error": f"找到 {occurrences} 处匹配，请提供更多上下文使匹配唯一，或设置 replace_all=true"
            }, ensure_ascii=False)

        if replace_all:
            new_content = content.replace(old_text, new_text)
        else:
            new_content = content.replace(old_text, new_text, 1)

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            return json.dumps({"success": False, "error": f"写入文件失败: {e}"}, ensure_ascii=False)

        return json.dumps({
            "success": True,
            "path": path,
            "replacements": occurrences if replace_all else 1,
            "message": f"成功替换 {occurrences if replace_all else 1} 处匹配"
        }, ensure_ascii=False)
