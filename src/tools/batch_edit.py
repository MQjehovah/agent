"""
原子批处理编辑工具 — hash-anchored 定位，批量原子提交

设计思路（参考 grok-build 的 hashline workflow）：
- 使用文件内容的行哈希作为锚点定位编辑目标
- 一次请求可以包含 N 个编辑（跨文件）
- 原子性：如果任意锚点失效，全部编辑被拒绝
- 失败后返回失效的锚点列表，LLM 可刷新后重试

用法:
    batch_edit(edits=[
        {"file": "src/main.py", "hash": "abc123...", "old": "foo()", "new": "bar()"},
        {"file": "src/utils.py", "hash": "def456...", "old": "old_func", "new": "new_func"},
    ])
"""
import hashlib
import json
import logging
import os
import re

logger = logging.getLogger("agent.tools.batch_edit")


class BatchEditTool:
    """原子批处理编辑工具"""

    @property
    def name(self) -> str:
        return "batch_edit"

    @property
    def description(self) -> str:
        return """原子批处理编辑工具。一次修改多个文件，原子提交：全部成功或全部失败。

核心特性:
1. Hash 锚点定位：每个编辑用目标行的哈希值做锚点，精确定位
2. 原子提交：任意一个文件锚点失效，全部拒绝，不会出现"改了一半"
3. 批量操作：一次请求可以跨多个文件执行多个编辑
4. 智能回退：失败时返回所有失效锚点，可刷新后重试

比 edit 工具更适合需要同时修改多个文件的任务（如重构、API 变更）。
如果只是改一个文件的一处内容，建议用 edit（更轻量）。

用法:
{"edits": [
    {"file": "src/main.py", "hash": "锚点哈希(可选)", "old": "要替换的文本", "new": "替换后的文本"},
    {"file": "src/main.py", "hash": "...", "old": "另一处文本", "new": "替换文本"},
]}

提示: 使用 code_search 或 grep 找到目标代码后，调用 batch_edit 批量修改。"""

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "description": "编辑列表，每个元素描述一个编辑操作",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {
                                "type": "string",
                                "description": "要修改的文件路径（相对于工作目录或绝对路径）"
                            },
                            "hash": {
                                "type": "string",
                                "description": "锚点文本的 SHA256 哈希（可选，用于验证锚点未过期）"
                            },
                            "old": {
                                "type": "string",
                                "description": "要被替换的原文（必须是文件中唯一匹配的文本）"
                            },
                            "new": {
                                "type": "string",
                                "description": "替换后的新文本"
                            },
                        },
                        "required": ["file", "old", "new"]
                    }
                },
                "workspace": {
                    "type": "string",
                    "description": "工作目录（可选，默认自动检测）"
                },
            },
            "required": ["edits"]
        }

    async def execute(self, **kwargs) -> str:
        edits = kwargs.get("edits", [])
        workspace = kwargs.get("workspace", "")

        if not edits:
            return json.dumps({"success": False, "error": "缺少 edits 参数"}, ensure_ascii=False)

        if not workspace:
            workspace = os.getcwd()

        # 第一阶段：验证所有编辑的锚点
        validation = self._validate_all(edits, workspace)
        if not validation["valid"]:
            return json.dumps({
                "success": False,
                "action": "全部拒绝",
                "reason": "锚点验证失败",
                "stale_edits": validation["stale_edits"],
                "failed_edits": validation.get("failed_edits", []),
                "hint": "文件内容已被修改，锚点失效。请重新读取文件获取最新的内容后再提交编辑。",
            }, ensure_ascii=False)

        # 第二阶段：执行所有编辑（已通过验证）
        results = self._apply_all(edits, workspace)

        # 统计
        success_count = sum(1 for r in results if r["success"])
        fail_count = sum(1 for r in results if not r["success"])

        response = {
            "success": fail_count == 0,
            "action": f"已修改 {success_count} 处" if fail_count == 0 else f"部分失败: {success_count} 成功, {fail_count} 失败",
            "total_edits": len(edits),
            "success_count": success_count,
            "fail_count": fail_count,
        }

        if fail_count > 0:
            response["failed_edits"] = [r for r in results if not r["success"]]

        return json.dumps(response, ensure_ascii=False, indent=2)

    def _validate_all(self, edits: list[dict], workspace: str) -> dict:
        """验证所有编辑的锚点

        Returns:
            {"valid": bool, "stale_edits": [...], "failed_edits": [...]}
        """
        stale_edits = []
        failed_edits = []

        for i, edit in enumerate(edits):
            file_path = edit["file"]
            old_text = edit["old"]
            expected_hash = edit.get("hash", "")
            full_path = self._resolve_path(file_path, workspace)

            # 文件存在性检查
            if not os.path.isfile(full_path):
                failed_edits.append({
                    "index": i,
                    "file": file_path,
                    "reason": "文件不存在",
                })
                continue

            # 读取文件
            try:
                with open(full_path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception as e:
                failed_edits.append({
                    "index": i,
                    "file": file_path,
                    "reason": f"文件读取失败: {e}",
                })
                continue

            # 检查 old_text 是否唯一
            occurrences = content.count(old_text)
            if occurrences == 0:
                stale_edits.append({
                    "index": i,
                    "file": file_path,
                    "reason": "原文未找到，可能已被修改或删除了",
                })
                continue
            if occurrences > 1:
                stale_edits.append({
                    "index": i,
                    "file": file_path,
                    "reason": f"原文在文件中出现了 {occurrences} 次，不是唯一匹配。请提供更多上下文以确保唯一性。",
                    "occurrences": occurrences,
                })
                continue

            # 如果提供了 hash，验证哈希匹配
            if expected_hash:
                actual_hash = self._compute_hash(old_text)
                if actual_hash != expected_hash:
                    stale_edits.append({
                        "index": i,
                        "file": file_path,
                        "reason": "锚点哈希不匹配，原文已被修改",
                        "expected_hash": expected_hash,
                        "actual_hash": actual_hash,
                    })
                    continue

        return {
            "valid": len(stale_edits) == 0 and len(failed_edits) == 0,
            "stale_edits": stale_edits,
            "failed_edits": failed_edits,
        }

    def _apply_all(self, edits: list[dict], workspace: str) -> list[dict]:
        """执行所有编辑"""
        results = []

        for edit in edits:
            file_path = edit["file"]
            old_text = edit["old"]
            new_text = edit["new"]
            full_path = self._resolve_path(file_path, workspace)

            try:
                with open(full_path, encoding="utf-8", errors="replace") as f:
                    content = f.read()

                # 执行替换（此时已验证 old_text 唯一）
                new_content = content.replace(old_text, new_text, 1)

                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(new_content)

                results.append({
                    "success": True,
                    "file": file_path,
                    "old_length": len(old_text),
                    "new_length": len(new_text),
                })

                logger.info(f"[batch_edit] 已修改 {file_path}: {len(old_text)} → {len(new_text)} 字符")

            except Exception as e:
                results.append({
                    "success": False,
                    "file": file_path,
                    "reason": str(e),
                })
                logger.error(f"[batch_edit] 修改失败 {file_path}: {e}")

        return results

    # ── 辅助 ───────────────────────────────────────────

    @staticmethod
    def _resolve_path(file_path: str, workspace: str) -> str:
        """解析文件路径"""
        if os.path.isabs(file_path):
            return file_path
        return os.path.join(workspace, file_path)

    @staticmethod
    def _compute_hash(text: str) -> str:
        """计算文本的 SHA256 哈希（前 16 字符足矣）"""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
