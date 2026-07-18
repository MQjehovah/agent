"""
Subagent 结果合成与冲突解决 — Contract-First 并行开发

设计思路（参考 grok-build 的 contract-first + 实时广播）：
- 主 Agent 在分解任务时先定义接口契约（contract）
- 各 Subagent 在契约约束下独立开发
- 更改共享接口时广播通知所有依赖方
- 多阶段合并：开发 → 集成 → 仲裁

用法:
    synthesizer = ResultSynthesizer(workspace)
    report = await synthesizer.synthesize(sub_results, contracts)
    # report = {
    #   "conflicts": [...], "auto_resolved": [...],
    #   "integration_status": "ok|conflicts",
    # }
"""
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("agent.synthesis")


@dataclass
class Contract:
    """接口契约 — 定义模块间的边界"""
    name: str                        # 契约名（如 "user_service"）
    module: str                      # 所属模块
    exposed_interfaces: list[str]    # 暴露的接口列表（函数签名）
    consumed_interfaces: list[str]   # 消费的其他模块接口
    data_types: list[str]            # 共享数据类型
    owner: str = ""                  # 负责的 Subagent
    status: str = "draft"            # draft / frozen / modified


@dataclass
class SubagentResult:
    """单个 Subagent 的执行结果"""
    agent_name: str
    module: str
    files_changed: list[str]
    contracts_defined: list[Contract]
    output_summary: str
    status: str                      # completed / failed / partial


@dataclass
class Conflict:
    """检测到的冲突"""
    type: str                        # interface / logic / duplicate / format
    severity: str                    # critical / high / medium / low
    description: str
    files: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    auto_resolvable: bool = False
    resolution: str = ""


class ResultSynthesizer:
    """结果合成器 — Contract-First 多 Agent 结果合并"""

    def __init__(self, workspace: str = ""):
        self.workspace = workspace
        self.conflicts: list[Conflict] = []
        self.contracts: dict[str, Contract] = {}

    async def synthesize(
        self,
        sub_results: list[SubagentResult],
        initial_contracts: list[Contract] = None,
    ) -> dict:
        """合成多个 Subagent 的结果

        Args:
            sub_results: Subagent 执行结果列表
            initial_contracts: 初始契约列表（主 Agent 分解时定义）

        Returns:
            合成报告
        """
        self.conflicts = []
        self.contracts = {}

        # 注册初始契约
        if initial_contracts:
            for c in initial_contracts:
                c.status = "frozen"
                self.contracts[c.name] = c

        # 收集所有结果中的契约声明
        for result in sub_results:
            for c in result.contracts_defined:
                if c.name in self.contracts:
                    # 检查是否与已有契约冲突
                    conflict = self._detect_contract_conflict(
                        self.contracts[c.name], c, result.agent_name
                    )
                    if conflict:
                        self.conflicts.append(conflict)
                else:
                    self.contracts[c.name] = c

        # 检测接口冲突
        interface_conflicts = await self._detect_interface_conflicts(sub_results)
        self.conflicts.extend(interface_conflicts)

        # 检测代码重复
        duplicate_conflicts = await self._detect_duplicates(sub_results)
        self.conflicts.extend(duplicate_conflicts)

        # 自动解决可解决的冲突
        auto_resolved = []
        remaining = []
        for conflict in self.conflicts:
            if conflict.auto_resolvable:
                await self._auto_resolve(conflict)
                auto_resolved.append(conflict)
            else:
                remaining.append(conflict)

        # 集成验证
        integration_status = await self._integration_verify(sub_results)

        return {
            "contracts": [c.__dict__ for c in self.contracts.values()],
            "conflicts": [c.__dict__ for c in remaining],
            "auto_resolved": [c.__dict__ for c in auto_resolved],
            "conflict_count": len(remaining),
            "auto_resolved_count": len(auto_resolved),
            "integration_status": integration_status,
            "summary": self._generate_summary(sub_results, remaining, auto_resolved),
        }

    def _detect_contract_conflict(
        self,
        existing: Contract,
        incoming: Contract,
        agent_name: str,
    ) -> Optional[Conflict]:
        """检测契约冲突"""
        if existing.status == "frozen" and existing.owner != agent_name:
            # 已冻结的契约被修改
            changed_interfaces = (
                set(incoming.exposed_interfaces) - set(existing.exposed_interfaces)
            )
            if changed_interfaces:
                return Conflict(
                    type="interface",
                    severity="high",
                    description=f"Subagent '{agent_name}' 尝试修改已冻结的契约 '{existing.name}'，"
                                f"变更接口: {changed_interfaces}",
                    files=[],
                    agents=[existing.owner or "unknown", agent_name],
                    auto_resolvable=False,
                    resolution="需开发者确认是否允许变更",
                )
        return None

    async def _detect_interface_conflicts(
        self, sub_results: list[SubagentResult]
    ) -> list[Conflict]:
        """检测接口冲突：同一函数被多个 Subagent 以不同签名实现"""
        conflicts = []

        # 收集所有函数定义
        func_defs: dict[str, list[dict]] = {}
        for result in sub_results:
            for file_path in result.files_changed:
                if not os.path.isfile(file_path):
                    continue
                try:
                    with open(file_path, encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    for m in re.finditer(
                        r"^(?:async\s+)?(?:def|class)\s+(\w+)\s*[\(:]",
                        content, re.MULTILINE
                    ):
                        name = m.group(1)
                        if name not in func_defs:
                            func_defs[name] = []
                        func_defs[name].append({
                            "file": file_path,
                            "agent": result.agent_name,
                            "line": content[:m.start()].count("\n") + 1,
                        })
                except Exception:
                    pass

        # 检查同名的多 Agent 定义
        for name, defs in func_defs.items():
            agents = set(d["agent"] for d in defs)
            if len(agents) > 1:
                conflicts.append(Conflict(
                    type="interface",
                    severity="high",
                    description=f"函数/类 '{name}' 被多个 Subagent 同时定义: {', '.join(agents)}",
                    files=[d["file"] for d in defs],
                    agents=list(agents),
                    auto_resolvable=False,
                    resolution=f"检查 {name} 的所属模块，保留正确的实现，删除冗余定义",
                ))

        return conflicts

    async def _detect_duplicates(
        self, sub_results: list[SubagentResult]
    ) -> list[Conflict]:
        """检测代码重复"""
        conflicts = []

        # 收集所有新文件的内容
        file_contents: dict[str, str] = {}
        for result in sub_results:
            for file_path in result.files_changed:
                if not os.path.isfile(file_path):
                    continue
                try:
                    with open(file_path, encoding="utf-8", errors="replace") as f:
                        file_contents[file_path] = f.read()
                except Exception:
                    pass

        # 逐对检查重复（只取前 50 行比较）
        files = list(file_contents.keys())
        for i in range(len(files)):
            for j in range(i + 1, len(files)):
                a_lines = file_contents[files[i]].split("\n")[:50]
                b_lines = file_contents[files[j]].split("\n")[:50]
                if len(a_lines) < 5 or len(b_lines) < 5:
                    continue

                # 计算相似度（简单 Jaccard）
                a_set = set(line.strip() for line in a_lines if line.strip())
                b_set = set(line.strip() for line in b_lines if line.strip())
                if not a_set or not b_set:
                    continue
                intersection = a_set & b_set
                union = a_set | b_set
                similarity = len(intersection) / len(union)

                if similarity > 0.6:  # 60% 相似视为重复
                    conflicts.append(Conflict(
                        type="duplicate",
                        severity="medium",
                        description=f"文件 '{files[i]}' 和 '{files[j]}' 高度相似 "
                                    f"(相似度 {similarity:.0%})",
                        files=[files[i], files[j]],
                        auto_resolvable=True,
                        resolution=f"考虑将重复代码提取为共享模块，或移除其中一个",
                    ))

        return conflicts

    async def _auto_resolve(self, conflict: Conflict) -> bool:
        """自动解决可解决的冲突"""
        if conflict.type == "duplicate":
            # 标记重复文件，由后续阶段处理
            conflict.resolution = f"已标记重复文件，建议提取共享模块"
            logger.info(f"[synthesis] 自动解决: {conflict.type} - {conflict.description[:80]}")
            return True
        return False

    async def _integration_verify(self, sub_results: list[SubagentResult]) -> str:
        """运行集成验证"""
        if not self.workspace or not os.path.isdir(self.workspace):
            return "skipped"

        try:
            # 检查语法
            py_files = []
            for result in sub_results:
                for f in result.files_changed:
                    if f.endswith(".py") and os.path.isfile(f):
                        py_files.append(f)

            if py_files:
                for f in py_files:
                    result = subprocess.run(
                        ["python", "-m", "py_compile", f],
                        capture_output=True, text=True, timeout=30,
                    )
                    if result.returncode != 0:
                        logger.warning(f"[synthesis] 语法检查失败: {f}: {result.stderr.strip()}")
                        return "has_errors"

            # 检查 git 状态
            if os.path.isdir(os.path.join(self.workspace, ".git")):
                result = subprocess.run(
                    ["git", "diff", "--stat"],
                    cwd=self.workspace, capture_output=True, text=True, timeout=10,
                )
                if result.stdout:
                    logger.info(f"[synthesis] 集成状态 ok: {result.stdout.strip()[:100]}")
                    return "ok"
                return "no_changes"

            return "ok"
        except Exception as e:
            logger.warning(f"[synthesis] 集成验证异常: {e}")
            return "verification_error"

    def _generate_summary(
        self,
        results: list[SubagentResult],
        remaining: list[Conflict],
        auto_resolved: list[Conflict],
    ) -> str:
        """生成人类可读的汇总"""
        completed = sum(1 for r in results if r.status == "completed")
        failed = sum(1 for r in results if r.status == "failed")
        total_files = sum(len(r.files_changed) for r in results)

        parts = [
            f"## 执行结果汇总",
            f"- Subagent: {len(results)} 个 ({completed} 完成, {failed} 失败)",
            f"- 文件变更: {total_files} 个",
        ]

        if remaining:
            parts.append(f"- 未解决冲突: {len(remaining)} 个 ⚠️")
            for c in remaining:
                parts.append(f"  - [{c.severity}] {c.type}: {c.description[:100]}")
        if auto_resolved:
            parts.append(f"- 自动解决: {len(auto_resolved)} 个 ✅")

        if not remaining:
            parts.append("- 冲突检查: 全部通过 ✅")

        return "\n".join(parts)

    @staticmethod
    def contract_from_dict(d: dict) -> Contract:
        """从字典创建 Contract"""
        return Contract(
            name=d.get("name", ""),
            module=d.get("module", ""),
            exposed_interfaces=d.get("exposed_interfaces", []),
            consumed_interfaces=d.get("consumed_interfaces", []),
            data_types=d.get("data_types", []),
            owner=d.get("owner", ""),
        )
