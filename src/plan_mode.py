"""
Plan Mode — 规划→审批→执行的三阶段工作流

设计思路（参考 grok-build Plan Mode）：
1. 检测：Agent 自动判断任务是否需要 Plan
2. 探索：只读扫描代码库（不改文件）
3. 规划：生成结构化计划（文件列表、命令、测试、验证步骤）
4. 审批：展示给用户，支持逐节点 approve/comment/discard
5. 执行：审批通过后执行

集成方式：
- 在 agent.run() 开始时调用 plan_mode.should_plan(task) 判断
- 需要规划时调用 plan_mode.generate_plan(task) 生成 Plan
- 等待用户确认后执行
"""
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("agent.plan")


@dataclass
class PlanStep:
    """计划中的一个步骤"""
    id: str = ""
    description: str = ""
    action: str = ""              # "read" / "edit" / "shell" / "test" / "verify"
    target: str = ""               # 目标文件/命令
    details: str = ""              # 详细说明
    status: str = "pending"        # pending / approved / rejected / completed
    estimated_tokens: int = 0
    risk: str = "low"              # low / medium / high


@dataclass
class Plan:
    """完整的执行计划"""
    title: str = ""
    task: str = ""
    summary: str = ""
    files_to_touch: list[str] = field(default_factory=list)
    commands_to_run: list[str] = field(default_factory=list)
    tests_to_write: list[str] = field(default_factory=list)
    steps: list[PlanStep] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    approved_at: float = 0.0
    status: str = "draft"          # draft / pending_approval / approved / rejected / executing / done

    @property
    def duration(self) -> float:
        if self.approved_at > 0:
            return self.approved_at - self.created_at
        return time.time() - self.created_at

    def to_markdown(self) -> str:
        """生成 Markdown 格式的计划"""
        lines = [
            f"# 执行计划: {self.title}",
            "",
            f"**摘要**: {self.summary}",
            "",
            "## 涉及文件",
        ]
        for f in self.files_to_touch:
            lines.append(f"- `{f}`")
        if self.commands_to_run:
            lines.extend(["", "## 需执行命令"])
            for c in self.commands_to_run:
                lines.append(f"- `{c}`")
        if self.tests_to_write:
            lines.extend(["", "## 需编写测试"])
            for t in self.tests_to_write:
                lines.append(f"- `{t}`")
        if self.steps:
            lines.extend(["", "## 执行步骤"])
            for i, step in enumerate(self.steps, 1):
                risk_icon = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(step.risk, "⚪")
                lines.append(f"{i}. {risk_icon} **{step.description}**")
                if step.details:
                    lines.append(f"   - {step.details}")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps({
            "title": self.title,
            "summary": self.summary,
            "files_to_touch": self.files_to_touch,
            "commands_to_run": self.commands_to_run,
            "tests_to_write": self.tests_to_write,
            "steps": [
                {"id": s.id, "description": s.description,
                 "action": s.action, "target": s.target, "risk": s.risk}
                for s in self.steps
            ],
            "status": self.status,
        }, ensure_ascii=False, indent=2)


class PlanMode:
    """Plan Mode 引擎

    判断任务是否需要规划，生成计划，管理审批流程。
    """

    # 不需要规划的任务类型（简单、常见操作）
    _SIMPLE_PATTERNS = [
        r"^(hi|hello|你好|在吗|你好吗)(\s|$)",
        r"^(是|否|对|不对|好|不好|可以|不可以)(\s|$)",
        r"^(谢谢|感谢|好的|知道了|明白|ok|yes|no)(\s|$)",
        r"^(查询|搜索|搜索一下|帮我查)(\s|$)",
        r"^(解释|说明|什么是|给我讲)(\s|$)",
        r"^\?$",
    ]

    # 始终需要规划的任务类型
    _COMPLEX_PATTERNS = [
        r"(重构|重写|refactor|rewrite)",
        r"(新功能|feature|add.*功能)",
        r"(架构|architecture|设计|design)",
        r"(修改|改|变更|change|update|modify)",
        r"(删除|remove|delete)",
        r"(迁移|migrate|migration)",
        r"(优化|optimize|perf|性能)",
        r"(集成|integrate|对接)",
        r"(创建项目|init|初始化)",
    ]

    def __init__(
        self,
        client=None,
        workspace: str = "",
        plan_dir: str = "",
        auto_plan: bool = True,
        require_approval: bool = True,
    ):
        self.client = client
        self.workspace = workspace
        self.plan_dir = plan_dir or os.path.join(workspace, ".agent", "plans") if workspace else ""
        self.auto_plan = auto_plan          # 自动判断是否进入 Plan Mode
        self.require_approval = require_approval  # 是否需要用户审批
        self.current_plan: Plan | None = None
        self._plan_history: list[Plan] = []
        # 外部注入的确认回调（由 TUI/CLI 设置）
        self.on_confirm: Optional[callable] = None

    def should_plan(self, task: str) -> bool:
        """判断任务是否需要 Plan Mode

        规则:
        - 匹配 Simple Patterns → 不需要
        - 匹配 Complex Patterns → 需要
        - 长度 > 200 字符 → 需要（任务包含太多指令）
        - 包含代码修改关键词 → 需要
        """
        if not self.auto_plan:
            return False

        task_lower = task.lower().strip()

        # 简单问候/查询 → 不需要
        for p in self._SIMPLE_PATTERNS:
            if re.match(p, task_lower):
                return False

        # 复杂任务 → 需要
        for p in self._COMPLEX_PATTERNS:
            if re.search(p, task_lower):
                return True

        # 长任务 → 需要
        if len(task) > 200:
            return True

        # 包含文件修改关键词 → 需要
        file_keywords = ["修改", "编辑", "写入", "创建", "改", "写一个", "新建"]
        if any(kw in task_lower for kw in file_keywords):
            return True

        return False

    async def generate_plan(self, task: str) -> Plan:
        """生成执行计划（调用 LLM 分析任务生成结构化计划）"""
        plan = Plan(title=task[:60], task=task)

        # 探测涉及的文件
        files = await self._explore_files(task)

        prompt = f"""你是一个软件工程规划专家。请分析以下任务并生成详细的执行计划。

## 任务
{task}

## 项目文件探测结果
{json.dumps(files, ensure_ascii=False, indent=2) if files else "（未探测）"}

## 输出格式
返回 JSON，包含以下字段：
```json
{{
  "summary": "计划摘要（1-2句话）",
  "files_to_touch": ["需要创建或修改的文件路径列表"],
  "commands_to_run": ["需要执行的命令列表"],
  "tests_to_write": ["需要编写的测试列表"],
  "steps": [
    {{
      "description": "步骤描述",
      "action": "read|edit|shell|test|verify",
      "target": "目标文件或命令",
      "details": "详细说明",
      "risk": "low|medium|high"
    }}
  ]
}}
```

## 规划原则
1. 优先读取文件理解现状再修改
2. 每个步骤应该独立可执行
3. 高风险操作（删除文件、修改关键配置）必须标记 risk=high
4. 如果任务非常简单，steps 可以只有 1-2 步
5. 复杂任务分解为 3-7 步最合适

只返回 JSON。"""

        try:
            if self.client:
                resp = await self.client.chat([{"role": "user", "content": prompt}])
                content = resp.choices[0].message.content if hasattr(resp, 'choices') else str(resp)
                content = content.strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                data = json.loads(content)
            else:
                # 无 LLM 客户端：用规则生成简单计划
                data = self._rule_based_plan(task, files)

            plan.summary = data.get("summary", f"执行任务: {task[:80]}")
            plan.files_to_touch = data.get("files_to_touch", [])
            plan.commands_to_run = data.get("commands_to_run", [])
            plan.tests_to_write = data.get("tests_to_write", [])

            for i, s in enumerate(data.get("steps", [])):
                plan.steps.append(PlanStep(
                    id=f"step-{i + 1}",
                    description=s.get("description", ""),
                    action=s.get("action", "edit"),
                    target=s.get("target", ""),
                    details=s.get("details", ""),
                    risk=s.get("risk", "low"),
                ))

        except Exception as e:
            logger.warning(f"Plan 生成失败，使用规则计划: {e}")
            data = self._rule_based_plan(task, files)
            plan.summary = data.get("summary", f"执行任务: {task[:80]}")
            for i, s in enumerate(data.get("steps", [])):
                plan.steps.append(PlanStep(id=f"step-{i + 1}", **s))

        plan.status = "pending_approval"
        self.current_plan = plan
        return plan

    async def _explore_files(self, task: str) -> list[dict]:
        """只读探测代码库中与任务相关的文件"""
        if not self.workspace or not os.path.isdir(self.workspace):
            return []

        files = []
        # 提取可能的关键文件路径
        path_patterns = re.findall(r'[\w/\\-]+\.\w+', task)
        for p in path_patterns:
            full = os.path.join(self.workspace, p)
            if os.path.isfile(full):
                try:
                    with open(full, encoding="utf-8", errors="replace") as f:
                        content = f.read(500)
                    files.append({"path": p, "exists": True, "preview": content[:200]})
                except Exception:
                    files.append({"path": p, "exists": True, "preview": "(无法读取)"})
            else:
                # 可能是新文件
                files.append({"path": p, "exists": False, "preview": ""})

        # 如果未提取到，用 grep 找相关代码
        if not files:
            try:
                import subprocess
                keywords = re.findall(r'[\w一-鿿]{2,}', task)
                keywords = [k for k in keywords if len(k) > 1][:5]
                for kw in keywords:
                    result = subprocess.run(
                        ["grep", "-rl", kw, "--include=*.py", "--include=*.ts", "--include=*.js",
                         "--include=*.rs", "--include=*.md", "."],
                        cwd=self.workspace,
                        capture_output=True, text=True, timeout=5,
                    )
                    for path in result.stdout.strip().split("\n"):
                        if path and len(files) < 10:
                            files.append({"path": path, "exists": True, "preview": ""})
            except Exception:
                pass

        return files[:15]

    def _rule_based_plan(self, task: str, files: list[dict]) -> dict:
        """无 LLM 时的规则兜底"""
        steps = [
            {"description": "阅读相关代码理解现状", "action": "read",
             "target": files[0]["path"] if files else "", "details": "先理解现有代码结构",
             "risk": "low"},
            {"description": "实现任务要求的修改", "action": "edit",
             "target": "", "details": "根据理解进行编码修改", "risk": "medium"},
            {"description": "运行测试验证修改", "action": "test",
             "target": "", "details": "确保修改不破坏现有功能", "risk": "low"},
        ]
        if not files:
            steps.insert(0, {"description": "探索项目结构", "action": "read",
                             "target": ".", "details": "了解项目目录结构", "risk": "low"})
        return {
            "summary": f"执行任务: {task[:100]}",
            "files_to_touch": [f["path"] for f in files if f["exists"]] or ["(待定)"],
            "commands_to_run": [],
            "tests_to_write": [],
            "steps": steps,
        }

    async def present_plan(self, plan: Plan) -> bool:
        """展示计划并等待用户审批

        Returns:
            True=已批准, False=已拒绝
        """
        if not self.require_approval:
            plan.status = "approved"
            plan.approved_at = time.time()
            return True

        markdown = plan.to_markdown()
        logger.info(f"=== Plan Mode ===\n{markdown}")

        if self.on_confirm:
            # 通过回调让 TUI/CLI 展示给用户
            result = await self.on_confirm(plan)
            if result:
                plan.status = "approved"
                plan.approved_at = time.time()
                return True
            else:
                plan.status = "rejected"
                return False

        # 无确认回调：默认通过（静默模式）
        plan.status = "approved"
        plan.approved_at = time.time()
        return True

    async def reject_and_rewrite(self, feedback: str) -> Plan:
        """根据用户反馈重新生成计划"""
        if not self.current_plan:
            return await self.generate_plan("")

        prompt = f"""用户拒绝了之前的执行计划，请根据反馈重新规划。

## 原始任务
{self.current_plan.task}

## 用户反馈
{feedback}

## 原计划摘要
{self.current_plan.summary}

请重新生成符合要求的执行计划。（输出格式同上）"""

        try:
            if self.client:
                resp = await self.client.chat([{"role": "user", "content": prompt}])
                content = resp.choices[0].message.content if hasattr(resp, 'choices') else str(resp)
                content = content.strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                data = json.loads(content)

                new_plan = Plan(title=self.current_plan.title, task=self.current_plan.task)
                new_plan.summary = data.get("summary", "")
                new_plan.files_to_touch = data.get("files_to_touch", [])
                new_plan.commands_to_run = data.get("commands_to_run", [])
                new_plan.tests_to_write = data.get("tests_to_write", [])
                for i, s in enumerate(data.get("steps", [])):
                    new_plan.steps.append(PlanStep(id=f"step-{i + 1}", **s))
                new_plan.status = "pending_approval"
                self.current_plan = new_plan
                return new_plan
        except Exception as e:
            logger.warning(f"重新规划失败: {e}")

        return self.current_plan

    def get_plan_history(self) -> list[dict]:
        """获取计划历史"""
        return [
            {
                "title": p.title,
                "summary": p.summary,
                "status": p.status,
                "duration_s": round(p.duration, 1),
                "step_count": len(p.steps),
            }
            for p in self._plan_history
        ]

    def complete_plan(self):
        """标记计划为已完成"""
        if self.current_plan:
            self.current_plan.status = "done"
            self._plan_history.append(self.current_plan)
            self.current_plan = None
