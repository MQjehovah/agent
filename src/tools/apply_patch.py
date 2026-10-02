"""统一 diff 应用工具 — git 风格 unified diff 的原子落地。

设计:
- 接受标准 unified diff（`--- a/x` / `+++ b/x` + `@@ -a,b +c,d @@` 若干 hunk）
- 先全量校验（路径、上下文、待删除行），任一失败则整体拒绝、不落盘
- 通过后逐文件拍快照（可撤销）再写回，保留原文件编码；换行统一为原文件主流风格
- `dry_run=true` 只做校验并返回 diff，不写盘
"""
import json
import logging
import os
import re
from dataclasses import dataclass, field

from . import BuiltinTool
from .edit import _make_diff, _read_text

logger = logging.getLogger("agent.tools")

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")


@dataclass
class _Hunk:
    old_start: int
    lines: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class _FilePatch:
    path: str
    hunks: list[_Hunk] = field(default_factory=list)


def _clean_path(header: str) -> str:
    """从 diff 头部提取路径，去掉 a/ b/ 前缀与制表符后缀。"""
    path = header.split("\t")[0].strip()
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return path


def _parse_patch(patch: str) -> tuple[list[_FilePatch], list[str]]:
    """解析 unified diff，返回 (文件补丁列表, 解析错误列表)。"""
    text = patch.replace("\r\n", "\n")
    if text.endswith("\n"):
        text = text[:-1]  # 去掉文本末尾换行产生的空行，避免误判为上下文空行
    lines = text.split("\n")
    files: list[_FilePatch] = []
    errors: list[str] = []
    current: _FilePatch | None = None
    hunk: _Hunk | None = None
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        if line.startswith("--- "):
            if i + 1 >= n or not lines[i + 1].startswith("+++ "):
                errors.append(f"第 {i + 1} 行: `---` 头部缺少对应的 `+++` 行")
                i += 1
                continue
            new_path = _clean_path(lines[i + 1][4:])
            i += 2
            if new_path == "/dev/null":
                errors.append("不支持删除文件的 patch（+++ 为 /dev/null）")
                current, hunk = None, None
                continue
            current = _FilePatch(path=new_path)
            files.append(current)
            hunk = None
            continue

        m = _HUNK_RE.match(line)
        if m:
            if current is None:
                errors.append(f"第 {i + 1} 行: hunk 出现在文件头之前")
                i += 1
                continue
            hunk = _Hunk(old_start=int(m.group(1)))
            current.hunks.append(hunk)
            i += 1
            continue

        if line.startswith("\\"):  # \ No newline at end of file
            i += 1
            continue

        if hunk is not None and line[:1] in (" ", "+", "-"):
            hunk.lines.append((line[0], line[1:]))
            i += 1
            continue

        if hunk is not None and line == "":
            # hunk 体中的空行（上下文空行常省掉前导空格）
            hunk.lines.append((" ", ""))
            i += 1
            continue

        # 其它行（diff --git / index / 元数据）忽略
        i += 1

    if not files and not errors:
        errors.append("未解析到任何文件 patch")
    return files, errors


def _apply_file(original_lines: list[str], hunks: list[_Hunk]) -> tuple[list[str] | None, str]:
    """按 hunk 应用补丁；成功返回 (新行列表, "")，失败返回 (None, 原因)。"""
    result: list[str] = []
    idx = 0
    for h in hunks:
        start = h.old_start - 1
        if start < idx:
            return None, f"hunk 起始行 {h.old_start} 与上一 hunk 重叠"
        while idx < start and idx < len(original_lines):
            result.append(original_lines[idx])
            idx += 1
        if idx < start:
            return None, f"hunk 起始行 {h.old_start} 超出文件范围"
        for op, text in h.lines:
            if op in (" ", "-"):
                actual = original_lines[idx] if idx < len(original_lines) else "<EOF>"
                if actual != text:
                    kind = "上下文" if op == " " else "待删除行"
                    return None, f"{kind}不匹配: 期望 {text!r}, 实际 {actual!r}"
                if op == " ":
                    result.append(original_lines[idx])
                idx += 1
            elif op == "+":
                result.append(text)
    while idx < len(original_lines):
        result.append(original_lines[idx])
        idx += 1
    return result, ""


class ApplyPatchTool(BuiltinTool):
    """统一 diff 原子应用工具"""

    @property
    def name(self) -> str:
        return "apply_patch"

    @property
    def description(self) -> str:
        return """应用 git 风格 unified diff（统一补丁）到工作区文件，原子提交。

特性:
1. 标准格式: `--- a/file` / `+++ b/file` + 若干 `@@ -a,b +c,d @@` hunk（上下文/删除/新增行）
2. 原子: 任一文件上下文不匹配或路径越界，则全部拒绝、不写入任何文件
3. 安全: 先校验后落盘；写入前自动快照，可用 undo 撤销
4. 预演: dry_run=true 仅校验并返回 diff，不写盘

用法:
{"patch": "--- a/src/main.py\\n+++ b/src/main.py\\n@@ -1,3 +1,3 @@\\n line1\\n-old\\n+new\\n line3\\n"}

规则:
- 路径相对工作区；超过一个 hunk 时请保持 hunk 头部行号正确
- 不支持创建/删除整个文件（仅改已存在文件）"""

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": "git 风格 unified diff 文本（含 --- / +++ / @@ hunk 头部）"
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "仅校验并返回 diff，不写盘（默认 false）",
                    "default": False
                },
            },
            "required": ["patch"]
        }

    async def execute(self, **kwargs) -> str:
        patch = kwargs.get("patch", "")
        dry_run = bool(kwargs.get("dry_run", False))

        if not patch or not patch.strip():
            return self._error("缺少 patch 参数")

        files, errors = _parse_patch(patch)
        if errors:
            return json.dumps({"success": False, "error": "补丁解析失败", "details": errors},
                              ensure_ascii=False, indent=2)

        prepared: list[tuple[str, str, str, str, str]] = []
        problems: list[str] = []
        for fp in files:
            if not fp.hunks:
                problems.append(f"{fp.path}: 未包含任何 hunk")
                continue
            full = self.resolve_path(fp.path)
            if not self.is_path_allowed(full):
                problems.append(f"{fp.path}: 路径超出工作目录范围")
                continue
            if not os.path.isfile(full):
                problems.append(f"{fp.path}: 文件不存在")
                continue
            try:
                old_content, encoding = _read_text(full)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{fp.path}: 读取失败: {e}")
                continue

            newline = "\r\n" if "\r\n" in old_content else "\n"
            trailing = old_content.endswith(("\n", "\r"))
            new_lines, err = _apply_file(old_content.splitlines(), fp.hunks)
            if err:
                problems.append(f"{fp.path}: {err}")
                continue
            new_content = newline.join(new_lines)
            if trailing:
                new_content += newline
            prepared.append((full, fp.path, old_content, new_content, encoding))

        if problems:
            return json.dumps({
                "success": False,
                "error": "补丁校验失败，未写入任何文件",
                "details": problems,
            }, ensure_ascii=False, indent=2)

        diffs = [
            {"file": rel, "diff": _make_diff(full, old, new)}
            for full, rel, old, new, _ in prepared
        ]

        if dry_run:
            return json.dumps({
                "success": True,
                "action": f"预演通过: {len(prepared)} 个文件",
                "dry_run": True,
                "files": diffs,
            }, ensure_ascii=False, indent=2)

        for full, _rel, old_content, new_content, encoding in prepared:
            await self._snapshot(full, old_content)
            try:
                with open(full, "w", encoding=encoding, newline="") as f:
                    f.write(new_content)
            except Exception as e:  # noqa: BLE001
                return self._error(f"写入文件失败 {full}: {e}")

        logger.info(f"[apply_patch] 已应用 {len(prepared)} 个文件")
        return json.dumps({
            "success": True,
            "action": f"已应用补丁: {len(prepared)} 个文件",
            "applied_files": [rel for _f, rel, _o, _n, _e in prepared],
            "files": diffs,
        }, ensure_ascii=False, indent=2)

    async def _snapshot(self, path: str, content: str):
        try:
            from agent.core import current_run
            from undo_manager import UndoManager
            rc = current_run()
            ws = self.workspace or (rc.task_dir if rc else "")
            if ws and os.path.exists(ws):
                await UndoManager(ws).snapshot_before_edit(path, content)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[apply_patch] 快照失败(忽略) {path}: {e}")

    @staticmethod
    def _error(msg: str) -> str:
        return json.dumps({"success": False, "error": msg}, ensure_ascii=False)
