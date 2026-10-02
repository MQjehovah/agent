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
    edit(path="src/main.py", old_string="foo()", new_string="bar()")

    # 带行号锚点的替换
    edit(path="src/main.py", old_string="foo()", new_string="bar()", line=42)

    # 多次替换（原子提交）
    edit(path="src/main.py", edits=[
        {"old_string": "foo()", "new_string": "bar()"},
        {"old_string": "old_func", "new_string": "new_func"},
    ])
"""
import difflib
import hashlib
import json
import logging
import os
import re

from . import BuiltinTool

logger = logging.getLogger("agent.tools")


def _content_lines(content: str) -> list[tuple[int, int, str]]:
    """把内容切成 (起始偏移, 结束偏移, 行文本) 列表。

    行文本不含行尾（\\n 与 \\r 均被剥离），偏移指向原始 content，供按行匹配后
    在原文上做精确替换——绝不重写未命中区域，从而保留 CRLF/行尾空白等原始字节。
    """
    lines: list[tuple[int, int, str]] = []
    for m in re.finditer(r"[^\n]*\n|[^\n]+$", content):
        raw = m.group()
        text = raw[:-1] if raw.endswith("\n") else raw
        if text.endswith("\r"):
            text = text[:-1]
        lines.append((m.start(), m.start() + len(text), text))
    return lines


def _split_old_lines(old: str) -> list[str]:
    """把 old_string 统一换行后切行（去掉末尾换行产生的空行）。"""
    text = old.replace("\r\n", "\n").replace("\r", "\n")
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n")


def _block_spans(content: str, old: str, collapse: bool) -> list[tuple[int, int]]:
    """按行匹配 old_string，返回原文中每处匹配的 (start, end) 偏移。

    collapse=False: 仅忽略行尾空白与换行差异；
    collapse=True : 折叠所有空白（容忍缩进差异），更宽松的兜底。
    """
    old_lines = _split_old_lines(old)
    if not old_lines or (len(old_lines) == 1 and old_lines[0] == ""):
        return []
    clines = _content_lines(content)
    n = len(old_lines)

    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip() if collapse else s.rstrip()

    want = [norm(line) for line in old_lines]
    spans: list[tuple[int, int]] = []
    for i in range(0, len(clines) - n + 1):
        if all(norm(clines[i + j][2]) == want[j] for j in range(n)):
            spans.append((clines[i][0], clines[i + n - 1][1]))
    return spans


def _locate_spans(content: str, old: str) -> list[tuple[int, int]]:
    """定位 old_string 在 content 中的所有匹配偏移。

    优先级：精确子串 → 行尾空白/换行容忍 → 全空白折叠。返回原文偏移，
    调用方据此在原字符串上替换，避免整文件规范化写回。
    """
    if not old:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        idx = content.find(old, start)
        if idx < 0:
            break
        spans.append((idx, idx + len(old)))
        start = idx + max(1, len(old))
    if spans:
        return spans
    spans = _block_spans(content, old, collapse=False)
    if spans:
        return spans
    return _block_spans(content, old, collapse=True)


def _line_of(content: str, offset: int) -> int:
    """返回偏移所在的 1 基行号。"""
    return content.count("\n", 0, offset) + 1


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


def _read_text(path: str) -> tuple[str, str]:
    """读取文本并返回 (内容, 编码)。

    优先 UTF-8，失败回退 GBK/CP936，最后 Latin-1；写回时沿用探测到的编码，
    避免把非 UTF-8 文件（如 Windows 本地编码）误读成乱码或改写成 UTF-8。
    """
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


class EditTool(BuiltinTool):
    """行级编辑工具 — SEARCH/REPLACE + 行锚点 + 批量/跨文件 + 备份 + diff"""

    # 可撤销的最大编辑数
    MAX_EDIT_HISTORY = 50

    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return """精确的文件编辑工具。支持 SEARCH/REPLACE、行号锚点、批量与跨文件原子编辑。

特性:
1. SEARCH/REPLACE: 提供 old_string（要替换的原文）和 new_string（替换后的内容）
2. 自动模糊匹配: old_string 自动处理缩进、空白、换行差异
3. 行号锚点: line 参数在命中多处时取最近一处
4. 批量编辑: edits 参数对同一文件多个编辑原子提交
5. 跨文件编辑: edits 每项带 file 即按文件分组，全部校验通过后一次写入
6. 自动备份 + diff 输出

使用规则:
- old_string 提供足够上下文使其唯一（推荐周围 2-3 行）
- 跨文件: edits=[{"file": "a.py", "old_string": "...", "new_string": "..."}, {"file": "b.py", ...}]
- 可选 hash: 对 old 文本的 SHA256 前 16 位，校验锚点未过期

单处编辑: {"path": "src/main.py", "old_string": "foo()", "new_string": "bar()"}
行号锚点: {"path": "src/main.py", "old_string": "foo()", "new_string": "bar()", "line": 42}
同文件批量: {"path": "src/main.py", "edits": [{"old_string": "foo()", "new_string": "bar()"}]}
跨文件批量: {"edits": [{"file": "a.py", "old_string": "x", "new_string": "y"}, {"file": "b.py", "old_string": "p", "new_string": "q"}]}"""

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要编辑的文件路径"
                },
                "old_string": {
                    "type": "string",
                    "description": "要被替换的原文（提供足够上下文使匹配唯一）"
                },
                "new_string": {
                    "type": "string",
                    "description": "替换后的新文本"
                },
                "line": {
                    "type": "integer",
                    "description": "行号锚点（可选，指定第几行附近进行匹配）",
                },
                "edits": {
                    "type": "array",
                    "description": "批量编辑列表（可选，与 old_string/new_string 互斥）；每项可带 file 做跨文件",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string", "description": "目标文件（跨文件编辑时使用；缺省用顶层 path）"},
                            "hash": {"type": "string", "description": "old 文本 SHA256 前 16 位（可选锚点校验）"},
                            "old_string": {"type": "string", "description": "要被替换的原文"},
                            "new_string": {"type": "string", "description": "替换后的新文本"},
                        },
                        "required": ["old_string", "new_string"]
                    }
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换所有匹配项（默认仅替换第一个匹配）",
                    "default": False
                },
                "workspace": {
                    "type": "string",
                    "description": "跨文件编辑时的基目录（可选，缺省用工作区）"
                }
            },
            "anyOf": [
                {"required": ["path", "old_string", "new_string"]},
                {"required": ["path", "edits"]},
                {"required": ["edits"]}
            ]
        }

    async def execute(self, **kwargs) -> str:
        path = kwargs.get("path", "")
        old_string = kwargs.get("old_string", "")
        new_string = kwargs.get("new_string", "")
        line = kwargs.get("line", 0)
        edits = kwargs.get("edits")
        replace_all = kwargs.get("replace_all", False)
        workspace = kwargs.get("workspace", "")

        # 批量模式：任一项带 file → 跨文件；否则视为同一文件（path 必填）
        if edits is not None:
            if not isinstance(edits, list) or not edits:
                return self._error("edits 必须为非空数组")
            if any(isinstance(e, dict) and (e.get("file") or e.get("path")) for e in edits):
                return await self._execute_multi_file(edits, workspace)
            if not path:
                return self._error("同文件批量编辑需要提供 path，或为每个 edit 指定 file")
            return await self._execute_batch(path, edits)

        # 单处编辑模式
        if not path:
            return self._error("文件路径不能为空")
        if not old_string or new_string is None:
            return json.dumps({
                "success": False,
                "error": "请提供 old_string 和 new_string，或使用 edits 进行批量编辑"
            }, ensure_ascii=False)

        return await self._execute_single(path, old_string, new_string, line, replace_all)

    async def _execute_single(self, path: str, old_string: str, new_string: str,
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
            content, encoding = _read_text(path)
        except Exception as e:
            return self._error(f"读取文件失败: {e}")

        # 在原文上定位（返回原始偏移），替换只动命中区间，保留其余原始字节
        spans = _locate_spans(content, old_string)
        if not spans:
            hint = self._build_mismatch_hint(content, old_string)
            return json.dumps({
                "success": False, "error": "未找到匹配的文本",
                "hint": hint
            }, ensure_ascii=False)

        if line and line > 0:
            # 行号锚点：命中多处时取离锚点最近的一处
            anchor = int(line)
            chosen = [min(spans, key=lambda s: abs(_line_of(content, s[0]) - anchor))]
        elif replace_all:
            chosen = list(spans)
        else:
            if len(spans) > 1:
                return json.dumps({
                    "success": False,
                    "error": f"找到 {len(spans)} 处匹配，请用 line 参数指定行号或设置 replace_all=true"
                }, ensure_ascii=False)
            chosen = spans

        # 逆序替换，偏移互不影响
        new_content = content
        for s, e in sorted(chosen, key=lambda s: s[0], reverse=True):
            new_content = new_content[:s] + new_string + new_content[e:]

        if new_content == content:
            return self._error("编辑后内容未发生变化（old_string 与 new_string 相同或未产生差异）")

        # 生成 diff
        diff = _make_diff(path, content, new_content)

        # 自动备份
        await self._backup(path, content)

        try:
            with open(path, "w", encoding=encoding, newline="") as f:
                f.write(new_content)
        except Exception as e:
            return self._error(f"写入文件失败: {e}")

        return json.dumps({
            "success": True,
            "path": path,
            "action": f"已修改 {path}",
            "replacements": len(chosen),
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
            original_content, encoding = _read_text(path)
        except Exception as e:
            return self._error(f"读取文件失败: {e}")

        # 逐个应用编辑（在原文上按偏移替换，保留未命中区域原始字节）
        content = original_content
        applied = 0
        for i, edit in enumerate(edits):
            old = edit.get("old_string", "")
            new = edit.get("new_string", "")
            if not old or new is None:
                continue

            spans = _locate_spans(content, old)
            if not spans:
                return json.dumps({
                    "success": False,
                    "error": f"第 {i+1} 个编辑未找到匹配: {old[:50]}",
                    "applied": applied
                }, ensure_ascii=False)

            s, e = spans[0]
            content = content[:s] + new + content[e:]
            applied += 1

        if content == original_content:
            return self._error("批量编辑未产生任何变化（old 与 new 相同或未产生差异）")

        # 生成 diff
        diff = _make_diff(path, original_content, content)

        # 自动备份原始内容
        await self._backup(path, original_content)

        try:
            with open(path, "w", encoding=encoding, newline="") as f:
                f.write(content)
        except Exception as e:
            return self._error(f"写入文件失败: {e}")

        return json.dumps({
            "success": True,
            "path": path,
            "action": f"批量修改完成: {applied}/{len(edits)} 处",
            "applied": applied,
            "total": len(edits),
            "diff": diff,
        }, ensure_ascii=False, indent=2)

    def _resolve_edit_path(self, file_path: str, workspace: str) -> str:
        """跨文件编辑的路径解析：显式 workspace 优先，否则用工具工作区。"""
        if workspace:
            p = file_path if os.path.isabs(file_path) else os.path.join(workspace, file_path)
            return os.path.normpath(p)
        return self.resolve_path(file_path)

    async def _execute_multi_file(self, edits: list[dict], workspace: str = "") -> str:
        """跨文件原子编辑：全部校验通过后一次写入。"""
        staged: dict[str, dict] = {}
        for i, edit in enumerate(edits):
            if not isinstance(edit, dict):
                return self._error(f"第 {i+1} 个编辑格式非法")
            file_path = str(edit.get("file") or edit.get("path") or "").strip()
            old = edit.get("old_string", "")
            new = edit.get("new_string", "")
            expected_hash = str(edit.get("hash") or "")
            if not file_path:
                return self._error(f"第 {i+1} 个编辑缺少 file")
            if not old or new is None:
                return self._error(f"第 {i+1} 个编辑缺少 old/new")
            full = self._resolve_edit_path(file_path, workspace)
            if not self.is_path_allowed(full):
                return self._error(f"第 {i+1} 个编辑路径超出工作目录范围: {file_path}")
            if full not in staged:
                if not os.path.isfile(full):
                    return self._error(f"第 {i+1} 个编辑文件不存在: {file_path}")
                try:
                    content, encoding = _read_text(full)
                except Exception as e:
                    return self._error(f"第 {i+1} 个编辑读取失败: {e}")
                staged[full] = {"content": content, "orig": content, "encoding": encoding}
            state = staged[full]
            if state["content"].count(old) != 1:
                return self._error(f"第 {i+1} 个编辑的 old 在文件中不是唯一匹配: {file_path}")
            if expected_hash and self._compute_hash(old) != expected_hash:
                return self._error(f"第 {i+1} 个编辑锚点哈希不匹配: {file_path}")
            state["content"] = state["content"].replace(old, new, 1)

        diffs = []
        for full, state in staged.items():
            if state["content"] == state["orig"]:
                continue
            base = self.workspace or workspace or os.getcwd()
            diffs.append({"file": os.path.relpath(full, base),
                          "diff": _make_diff(full, state["orig"], state["content"])})
            await self._backup(full, state["orig"])
            try:
                with open(full, "w", encoding=state["encoding"], newline="") as f:
                    f.write(state["content"])
            except Exception as e:
                return self._error(f"写入文件失败 {full}: {e}")

        if not diffs:
            return self._error("批量编辑未产生任何变化（old 与 new 相同或未产生差异）")
        return json.dumps({
            "success": True,
            "action": f"跨文件批量修改完成: {len(diffs)} 个文件",
            "applied_files": [d["file"] for d in diffs],
            "diffs": diffs,
        }, ensure_ascii=False, indent=2)

    @staticmethod
    def _compute_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    # ── 备份 ─────────────────────────────────────────

    async def _backup(self, path: str, content: str):
        """编辑前自动备份"""
        try:
            from agent.core import current_run
            from undo_manager import UndoManager
            rc = current_run()
            ws = self.workspace or (rc.task_dir if rc else "")
            if ws and os.path.exists(ws):
                mgr = UndoManager(ws)
                await mgr.snapshot_before_edit(path, content)
        except Exception as e:
            logger.debug(f"自动备份失败: {e}")

    # ── 辅助 ─────────────────────────────────────────

    def _build_mismatch_hint(self, content: str, old_string: str) -> str:
        """匹配失败时提供附近内容的提示"""
        if not old_string.strip():
            return ""
        first_line = old_string.strip().split("\n")[0].strip()
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
                f"请对比 old_string 与文件实际内容，注意空白、缩进、引号等差异。"
            )
        return ""

    @staticmethod
    def _error(msg: str) -> str:
        return json.dumps({"success": False, "error": msg}, ensure_ascii=False)
