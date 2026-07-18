"""
代码变更影响分析 — 结构化 Diff

设计思路（参考 grok-build 的 grok diff --summary）：
- 分析 git diff 生成结构化影响报告
- 按功能模块对变更分组
- 标注每个文件的风险等级
- 追踪受影响的调用方数量

用法:
    analyzer = CodeDiffAnalyzer(workspace)
    report = await analyzer.analyze()
    # report = {
    #     "files": [...],
    #     "high_risk": [...],
    #     "summary": "3 files changed, 2 high risk",
    # }
"""
import json
import logging
import os
import re
import subprocess

logger = logging.getLogger("agent.code_diff")


class CodeDiffAnalyzer:
    """代码变更影响分析"""

    # 高风险变更模式
    HIGH_RISK_PATTERNS = [
        # API/接口变更
        r"(def |async def |class |fun |fn |func )",  # 函数签名变更
        r"(def |async def ).*\(.*\).*:",               # 参数变更
        # 数据库变更
        r"(ALTER TABLE|CREATE TABLE|DROP TABLE|ALTER COLUMN)",
        # 配置变更
        r"(config|setting|environment|env)",
        # 依赖变更
        r"(import |from |require|include )",
        # 安全相关
        r"(password|secret|token|auth|permission)",
    ]

    def __init__(self, workspace: str):
        self.workspace = workspace

    async def analyze(self, diff_text: str = "") -> dict:
        """分析代码变更，生成结构化影响报告

        Args:
            diff_text: 可选的 diff 文本（为空则自动获取 git diff）

        Returns:
            结构化影响报告
        """
        if not diff_text:
            diff_text = self._get_git_diff()

        if not diff_text:
            return {"files": [], "summary": "无变更", "high_risk_count": 0}

        # 按文件分组 diff
        files = self._parse_diff_by_file(diff_text)

        # 分析每个文件的影响
        analyzed_files = []
        for file_diff in files:
            analysis = await self._analyze_file(file_diff)
            analyzed_files.append(analysis)

        # 汇总
        high_risk = [f for f in analyzed_files if f["risk"] == "high"]
        medium_risk = [f for f in analyzed_files if f["risk"] == "medium"]

        report = {
            "files": analyzed_files,
            "high_risk": high_risk,
            "medium_risk": medium_risk,
            "high_risk_count": len(high_risk),
            "medium_risk_count": len(medium_risk),
            "total_files": len(analyzed_files),
            "summary": self._generate_summary(analyzed_files),
            "diff_preview": self._generate_diff_preview(diff_text),
        }

        return report

    def _parse_diff_by_file(self, diff_text: str) -> list[dict]:
        """按文件分组解析 diff"""
        files = []
        current_file = None
        current_lines = []

        for line in diff_text.split("\n"):
            if line.startswith("diff --git"):
                # 保存上一个文件
                if current_file:
                    files.append({
                        "file": current_file,
                        "diff": "\n".join(current_lines),
                        "lines": current_lines,
                    })
                # 解析文件名
                m = re.match(r"diff --git a/(.+?) b/(.+?)$", line)
                current_file = m.group(2) if m else "unknown"
                current_lines = [line]
            else:
                current_lines.append(line)

        # 最后一个文件
        if current_file and current_lines:
            files.append({
                "file": current_file,
                "diff": "\n".join(current_lines),
                "lines": current_lines,
            })

        return files

    async def _analyze_file(self, file_diff: dict) -> dict:
        """分析单个文件的变更"""
        file_path = file_diff["file"]
        lines = file_diff["lines"]

        # 变更类型
        change_type = self._detect_change_type(lines)

        # 变更统计
        additions, deletions = self._count_changes(lines)

        # 风险等级
        risk = self._assess_risk(file_path, lines)

        # 变更内容摘要
        change_summary = self._summarize_changes(lines)

        # 影响的调用方
        callers = self._find_affected_callers(file_path, lines)

        return {
            "file": file_path,
            "change_type": change_type,
            "additions": additions,
            "deletions": deletions,
            "risk": risk,
            "summary": change_summary,
            "callers_affected": callers,
            "has_signature_change": self._has_signature_change(lines),
            "has_test_changes": "test" in file_path.lower() or "spec" in file_path.lower() or "tests" in file_path.lower(),
        }

    @staticmethod
    def _detect_change_type(lines: list[str]) -> str:
        """检测变更类型"""
        for line in lines:
            if line.startswith("new file mode"):
                return "added"
            if line.startswith("deleted file mode"):
                return "deleted"
            if line.startswith("rename from"):
                return "renamed"
        # 检查是否有新行
        has_additions = any(line.startswith("+") and not line.startswith("+++") for line in lines)
        has_deletions = any(line.startswith("-") and not line.startswith("---") for line in lines)
        if has_additions and has_deletions:
            return "modified"
        if has_additions:
            return "added_content"
        if has_deletions:
            return "deleted_content"
        return "modified"

    @staticmethod
    def _count_changes(lines: list[str]) -> tuple[int, int]:
        """统计增删行数"""
        additions = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
        deletions = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
        return additions, deletions

    def _assess_risk(self, file_path: str, lines: list[str]) -> str:
        """评估风险等级"""
        # 新增文件 → 低风险
        if self._detect_change_type(lines) == "added":
            return "low"

        # 删除文件 → 高风险
        if self._detect_change_type(lines) == "deleted":
            return "high"

        # 检查高风险模式
        for pattern in self.HIGH_RISK_PATTERNS:
            for line in lines:
                if line.startswith("+") and re.search(pattern, line, re.IGNORECASE):
                    return "high"

        # 大量行变更 → 中风险
        additions, deletions = self._count_changes(lines)
        if additions + deletions > 50:
            return "medium"

        # 测试文件 → 低风险
        if "test" in file_path.lower() or "spec" in file_path.lower():
            return "low"

        return "low"

    def _summarize_changes(self, lines: list[str]) -> str:
        """生成变更摘要"""
        added_lines = []
        deleted_lines = []

        for line in lines:
            if line.startswith("+") and not line.startswith("+++"):
                added_lines.append(line[1:].strip())
            elif line.startswith("-") and not line.startswith("---"):
                deleted_lines.append(line[1:].strip())

        summary_parts = []
        if added_lines:
            # 取前 3 条新增内容
            preview = added_lines[:3]
            summary_parts.append(f"新增({len(added_lines)}行): {'; '.join(preview)}")
        if deleted_lines:
            preview = deleted_lines[:3]
            summary_parts.append(f"删除({len(deleted_lines)}行): {'; '.join(preview)}")

        return "; ".join(summary_parts) if summary_parts else "细节变更"

    def _find_affected_callers(self, file_path: str, lines: list[str]) -> list[dict]:
        """找受影响的调用方"""
        # 提取被修改的符号名
        changed_symbols = []
        for line in lines:
            if line.startswith("+") or line.startswith("-"):
                # 匹配函数/类定义行
                m = re.match(r"[+-]\s*(?:export\s+)?(?:async\s+)?(?:def|class|fun|fn|func)\s+(\w+)", line)
                if m:
                    changed_symbols.append(m.group(1))
                # 匹配函数调用
                m = re.findall(r'(\w+)\s*\(', line)
                changed_symbols.extend(m)

        if not changed_symbols:
            return []

        # 用 grep 找调用方
        callers = []
        for sym in set(changed_symbols[:5]):  # 最多查 5 个符号
            try:
                sym_escaped = re.escape(sym)
                # 跳过自身的文件
                result = subprocess.run(
                    ["grep", "-rn", f"\\b{sym_escaped}\\(", ".", "--include=*.py",
                     "--include=*.ts", "--include=*.js", "--exclude-dir=.git",
                     "--exclude-dir=node_modules", "--exclude-dir=.venv"],
                    cwd=self.workspace, capture_output=True, text=True, timeout=15,
                )
                for line in result.stdout.strip().split("\n"):
                    if not line:
                        continue
                    if file_path in line and ":" in line:
                        continue  # 排除自身
                    if ":" in line:
                        parts = line.split(":", 2)
                        if len(parts) >= 2:
                            callers.append({
                                "file": parts[0],
                                "line": parts[1],
                                "symbol": sym,
                                "code": parts[2].strip() if len(parts) > 2 else "",
                            })
            except Exception:
                pass

        return callers[:20]  # 最多返回 20 个

    @staticmethod
    def _has_signature_change(lines: list[str]) -> bool:
        """检查是否有函数签名变更"""
        for line in lines:
            if line.startswith("+"):
                if re.match(r"[+-]\s*(def |async def |class |fun |fn |func )", line):
                    return True
        return False

    def _get_git_diff(self) -> str:
        """获取 git diff 输出"""
        if not os.path.isdir(os.path.join(self.workspace, ".git")):
            return ""
        try:
            result = subprocess.run(
                ["git", "diff"],
                cwd=self.workspace, capture_output=True, text=True, timeout=10,
            )
            return result.stdout
        except Exception as e:
            logger.debug(f"git diff 失败: {e}")
            return ""

    @staticmethod
    def _generate_summary(files: list[dict]) -> str:
        """生成人类可读的总结"""
        high = sum(1 for f in files if f["risk"] == "high")
        medium = sum(1 for f in files if f["risk"] == "medium")
        low = sum(1 for f in files if f["risk"] == "low")
        total_additions = sum(f.get("additions", 0) for f in files)
        total_deletions = sum(f.get("deletions", 0) for f in files)
        changed_types = {}
        for f in files:
            t = f.get("change_type", "modified")
            changed_types[t] = changed_types.get(t, 0) + 1

        type_str = ", ".join(f"{v} {k}" for k, v in sorted(changed_types.items()))
        return (f"共 {len(files)} 个文件变更 ({type_str}), "
                f"+{total_additions}/-{total_deletions} 行, "
                f"高风险:{high} 中风险:{medium} 低风险:{low}")

    @staticmethod
    def _generate_diff_preview(diff_text: str, max_chars: int = 2000) -> str:
        """生成 diff 预览"""
        if len(diff_text) <= max_chars:
            return diff_text
        return diff_text[:max_chars] + "\n... [diff 过长，已截断]"
