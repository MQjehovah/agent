"""
行级编辑工具 — SEARCH/REPLACE diff 块 + 自动备份 + 行号锚点

设计思路（参考 Claude Code 的 SEARCH/REPLACE + Grok Build 的 hash-anchor）：
- SEARCH/REPLACE 块: LLM 提供要替换的原文和替换后的新文，工具自动模糊匹配
- 行号锚点: 可选的 line_number 参数，精确定位编辑行
- 自动备份: 编辑前自动拍快照，支持撤销
- diff 输出: 编辑后返回 unified diff，LLM 可见变化
- 多次替换: 支持一次性在同一个文件中执行多个 SEARCH/REPLACE 块

用法:
    # 单处替换
    edit(path="src/main.py", old_text="foo()", new_text="bar()")

    # 带行号锚点的替换
    edit(path="src/main.py", old_text="foo()", new_text="bar()", line=42)

    # 多次替换（原子提交）
    edit(path="src/main.py", edits=[
        {"old": "foo()", "new": "bar()"},
        {"old": "old_func", "new": "new_func"},
    ])
"""
import difflib
import json
import logging
import os
import re

from . import BuiltinTool

logger = logging.getLogger("agent.tools")


def _normalize(text: str) -> str:
    """标准化文本用于匹配（统一换行、去掉行尾空白）"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines)


def _make_diff(file_path: str, old_content: str, new_content: str) -> str:
    """生成 unified diff 用于展示"""
    rel_path = os.path.basename(file_path)
    diff = difflib.unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
    )
    return "".join(diff)


class EditTool(BuiltinTool):
    """行级编辑工具 — SEARCH/REPLACE + 自动备份 + diff 输出"""

    # 可撤销的最大编辑数
    MAX_EDIT_HISTORY = 50

    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return """精确的行级文件编辑工具。支持 SEARCH/REPLACE 模式、行号锚点、批量编辑。

特性:
1. SEARCH/REPLACE: 提供 old_text（文件中要替换的原文）和 new_text（替换后的内容）
2. 自动模糊匹配: old_text 会自动处理缩进、空白、换行差异，不需要精确到字符
3. 行号锚点: 可指定 line 参数精确定位到某行
4. 批量编辑: 一次调用可对同一文件执行多个编辑（edits 参数）
5. 自动备份: 每次编辑前自动备份，可通过 undo 工具撤销
6. diff 输出: 返回 unified diff 展示具体变化

使用规则:
- old_text 提供足够上下文使其在文件中唯一（推荐周围 2-3 行代码）
- 修改前建议先用 file(read) 确认文件内容
- 批量编辑（edits 参数）是原子的：全部成功或全部失败

单处编辑: {"path": "src/main.py", "old_text": "foo()", "new_text": "bar()"}
行号锚点: {"path": "src/main.py", "old_text": "foo()", "new_text": "bar()", "line": 42}
批量编辑: {"path": "src/main.py", "edits": [{"old": "foo()", "new": "bar()"}, ...]}"""

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要编辑的文件路径"
                },
                "old_text": {
                    "type": "string",
                    "description": "要被替换的原文（提供足够上下文使匹配唯一）"
                },
                "new_text": {
                    "type": "string",
                    "description": "替换后的新文本"
                },
                "line": {
                    "type": "integer",
                    "description": "行号锚点（可选，指定第几行附近进行匹配）",
                },
                "edits": {
                    "type": "array",
                    "description": "批量编辑列表（可选，与 old_text/new_text 互斥）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old": {"type": "string", "description": "要被替换的原文"},
                            "new": {"type": "string", "description": "替换后的新文本"},
                        },
                        "required": ["old", "new"]
                    }
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换所有匹配项（默认仅替换第一个匹配）",
                    "default": False
                }
            },
            "anyOf": [
                {"required": ["path", "old_text", "new_text"]},
                {"required": ["path", "edits"]}
            ]
        }

    async def execute(self, **kwargs) -> str:
        path = kwargs.get("path", "")
        old_text = kwargs.get("old_text", "")
        new_text = kwargs.get("new_text", "")
        line = kwargs.get("line", 0)
        edits = kwargs.get("edits", None)
        replace_all = kwargs.get("replace_all", False)

        if not path:
            return json.dumps({"success": False, "error": "文件路径不能为空"}, ensure_ascii=False)

        # 批量模式
        if edits is not None:
            return await self._execute_batch(path, edits)

        # 单处编辑模式
        if not old_text or new_text is None:
            return json.dumps({
                "success": False,
                "error": "请提供 old_text 和 new_text，或使用 edits 进行批量编辑"
            }, ensure_ascii=False)

        return await self._execute_single(path, old_text, new_text, line, replace_all)

    async def _execute_single(self, path: str, old_text: str, new_text: str,
                               line: int = 0, replace_all: bool = False) -> str:
        """单处编辑"""
        path = self.resolve_path(path)
        if not self.is_path_allowed(path):
            return self._error("路径超出工作目录范围")

        if not os.path.exists(path):
            return self._error(f"文件不存在: {path}")
        if os.path.isdir(path):
            return self._error(f"路径是目录: {path}")

        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            return self._error(f"读取文件失败: {e}")

        normalized_content = _normalize(content)
        normalized_old = _normalize(old_text)

        # 行号锚点辅助匹配
        match_start = self._find_match(normalized_content, normalized_old, line)
        if match_start is None:
            hint = self._build_mismatch_hint(content, old_text)
            return json.dumps({
                "success": False, "error": "未找到匹配的文本",
                "hint": hint
            }, ensure_ascii=False)

        # 检查是否唯一
        count = list(self._find_all_matches(normalized_content, normalized_old))
        if len(count) > 1 and not replace_all:
            if not line:
                return json.dumps({
                    "success": False, "error": f"找到 {len(count)} 处匹配，请用 line 参数指定行号或设置 replace_all=true"
                }, ensure_ascii=False)

        # 执行替换
        normalized_new = _normalize(new_text)
        if replace_all:
            new_normalized = normalized_content.replace(normalized_old, normalized_new)
        else:
            new_normalized = normalized_content.replace(normalized_old, normalized_new, 1)

        # 生成 diff
        diff = _make_diff(path, content, new_normalized)

        # 自动备份
        await self._backup(path, content)

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_normalized)
        except Exception as e:
            return self._error(f"写入文件失败: {e}")

        return json.dumps({
            "success": True,
            "path": path,
            "action": f"已修改 {path}",
            "diff": diff,
            "hint": "如需撤销此修改，请使用 edit 工具还原，或通过 git checkout 恢复"
        }, ensure_ascii=False, indent=2)

    async def _execute_batch(self, path: str, edits: list[dict]) -> str:
        """批量编辑（原子提交）"""
        path = self.resolve_path(path)
        if not self.is_path_allowed(path):
            return self._error("路径超出工作目录范围")
        if not os.path.exists(path):
            return self._error(f"文件不存在: {path}")
        if os.path.isdir(path):
            return self._error(f"路径是目录: {path}")

        # 读取原始内容
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                original_content = f.read()
        except Exception as e:
            return self._error(f"读取文件失败: {e}")

        # 逐个应用编辑
        content = original_content
        applied = []
        for i, edit in enumerate(edits):
            old = edit.get("old", "")
            new = edit.get("new", "")
            if not old or new is None:
                continue

            normalized_content = _normalize(content)
            normalized_old = _normalize(old)

            match_start = self._find_match(normalized_content, normalized_old, 0)
            if match_start is None:
                return json.dumps({
                    "success": False,
                    "error": f"第 {i+1} 个编辑未找到匹配: {old[:50]}",
                    "applied": i
                }, ensure_ascii=False)

            normalized_new = _normalize(new)
            content = normalized_content.replace(normalized_old, normalized_new, 1)
            applied.append(i + 1)

        # 生成 diff
        diff = _make_diff(path, original_content, content)

        # 自动备份原始内容
        await self._backup(path, original_content)

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return self._error(f"写入文件失败: {e}")

        return json.dumps({
            "success": True,
            "path": path,
            "action": f"批量修改完成: {len(applied)}/{len(edits)} 处",
            "applied": len(applied),
            "total": len(edits),
            "diff": diff,
        }, ensure_ascii=False, indent=2)

    # ── 匹配逻辑 ─────────────────────────────────────

    def _find_match(self, content: str, old_text: str, line: int = 0) -> int | None:
        """在 content 中查找 old_text，返回匹配起始位置或 None"""
        if line > 0:
            # 行号锚点模式：只在指定行附近搜索
            content_lines = content.split("\n")
            start_line = max(0, line - 3)
            end_line = min(len(content_lines), line + 2)
            old_lines = old_text.split("\n")
            search_len = sum(len(l) + 1 for l in content_lines[:end_line])
            start_offset = sum(len(l) + 1 for l in content_lines[:start_line])
            search_zone = content[start_offset:search_len]
            idx = search_zone.find(old_text)
            if idx >= 0:
                return start_offset + idx
            # 行号锚点放宽匹配：只匹配第一行
            first_line = old_lines[0].strip() if old_lines else ""
            if first_line:
                for i in range(start_line, min(end_line, len(content_lines))):
                    if first_line in content_lines[i]:
                        return sum(len(l) + 1 for l in content_lines[:i])
            return None

        # 普通模式：全局匹配
        idx = content.find(old_text)
        if idx >= 0:
            return idx

        # 放宽匹配：去空白后尝试
        stripped_content = re.sub(r'\s+', ' ', content)
        stripped_old = re.sub(r'\s+', ' ', old_text)
        idx = stripped_content.find(stripped_old)
        if idx >= 0:
            return idx

        return None

    def _find_all_matches(self, content: str, old_text: str) -> list[int]:
        """返回所有匹配位置"""
        positions = []
        start = 0
        while True:
            idx = content.find(old_text, start)
            if idx < 0:
                break
            positions.append(idx)
            start = idx + 1
        return positions

    # ── 备份 ─────────────────────────────────────────

    async def _backup(self, path: str, content: str):
        """编辑前自动备份"""
        try:
            from worker.undo_manager import UndoManager
            from agent.context import current_run
            rc = current_run()
            ws = self.workspace or (rc.task_dir if rc else "")
            if ws and os.path.exists(ws):
                mgr = UndoManager(ws)
                await mgr.snapshot_before_edit(path, content)
        except Exception as e:
            logger.debug(f"自动备份失败: {e}")

    # ── 辅助 ─────────────────────────────────────────

    def _build_mismatch_hint(self, content: str, old_text: str) -> str:
        """匹配失败时提供附近内容的提示"""
        if not old_text.strip():
            return ""
        first_line = old_text.strip().split("\n")[0].strip()
        if not first_line:
            return ""

        content_lines = content.split("\n")
        clean_key = re.sub(r'[^\w]', ' ', first_line[:50])
        keywords = [w for w in clean_key.split() if len(w) > 2]

        best_score = 0.0
        best_line = 0
        best_text = ""

        for i, line in enumerate(content_lines):
            if not keywords:
                from difflib import SequenceMatcher
                ratio = SequenceMatcher(None, line.strip(), first_line).ratio()
                if ratio > best_score:
                    best_score = ratio
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
                    from difflib import SequenceMatcher
                    ratio = SequenceMatcher(None, line.strip(), first_line).ratio()
                    score = ratio + 0.2 * match_count
                    if score > best_score:
                        best_score = score
                        best_line = i + 1
                        start = max(0, i - 1)
                        end = min(len(content_lines), i + 3)
                        best_text = "\n".join(
                            f"  {start + j + 1:6d}\t{content_lines[start + j]}"
                            for j in range(end - start)
                        )

        if best_score > 0.3 and best_text:
            return (
                f"最相似的内容在第 {best_line} 行附近：\n{best_text}\n"
                f"请对比 old_text 与文件实际内容，注意空白、缩进、引号等差异。"
            )
        return ""

    @staticmethod
    def _error(msg: str) -> str:
        return json.dumps({"success": False, "error": msg}, ensure_ascii=False)
