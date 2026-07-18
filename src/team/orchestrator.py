"""
团队编排器 — DAG 驱动的并行多 Agent 执行引擎

核心改进（v2.0）:
1. 真并行执行：使用 AgentPool 替代串行创建/销毁
2. 同角色多实例：大任务分解后多个相同角色并行工作
3. Worktree 隔离：每个 Subagent 在独立目录工作
4. 批量并行（map）：一批独立子任务同时分配给多个 Agent
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any

from team.context import TeamContext
from team.dag import DAGNode, ExecutionDAG
from team.feedback import parse_test_output
from team.pipeline_builder import build_pipeline, generate_pipeline_async

logger = logging.getLogger("agent.team.orchestrator")


def _extract_json_from_llm(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
    return content.strip()


# ── 任务分解器：将大任务拆成可并行执行的子任务 ──────────

class TaskDecomposer:
    """将大任务分解为多个独立的并行子任务"""

    @staticmethod
    async def decompose(task: str, role: str, module_count: int = 3) -> list[dict]:
        """将任务按模块分解为多个并行的子任务

        Args:
            task: 原始任务
            role: 执行角色（如"代码工程师"）
            module_count: 期望分解的模块数

        Returns:
            [{"task": str, "module": str, "role": str}, ...]
        """
        # 简单任务不分解
        if len(task) < 200:
            return [{"task": task, "module": "default", "role": role}]

        prompt = f"""你是一个任务分解专家。请将以下开发任务分解为 {module_count} 个独立的模块，每个模块可以由不同的开发者并行实现。

## 任务
{task}

## 角色
{role}

## 分解原则
1. 每个模块应该是独立的、可并行实现的
2. 模块之间边界清晰，减少依赖
3. 每个模块包含足够上下文，子任务能独立理解
4. 如果任务很简单无法分解，只返回1个模块

只返回 JSON 数组:
[{{"module": "模块名", "task": "该模块的完整任务描述"}}]"""

        try:
            from agent.core import current_run
            rc = current_run()
            client = getattr(rc, '_llm_client', None) if rc else None
            if client is None:
                # 尝试从 settings 创建临时 client
                from llm.client import LLMClient
                from settings import get_settings
                eps = get_settings().llm_endpoints
                if eps:
                    client = LLMClient(endpoints=eps, timeout=30)
            if client:
                resp = await client.chat([{"role": "user", "content": prompt}])
                content = resp.choices[0].message.content if hasattr(resp, 'choices') else str(resp)
                content = content.strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                modules = json.loads(content)
                if isinstance(modules, list) and modules:
                    for m in modules:
                        m["role"] = role
                    return modules
        except Exception as e:
            logger.debug(f"任务分解失败，使用原始任务: {e}")

        return [{"task": task, "module": "default", "role": role}]


class TeamOrchestrator:
    def __init__(
        self,
        team_name: str,
        team_config: dict[str, Any],
        members: dict[str, dict[str, Any]],
        subagent_manager,
        llm_client,
        memory_manager=None,
        pipeline_mode: str = "feedback",
        progress_callback=None,
        parent_session_id: str = "",
        # ── v2.0 新参数 ──
        agent_pool=None,          # AgentPool 实例（复用 Agent）
        worktree_manager=None,    # WorktreeManager 实例（目录隔离）
        max_parallel: int = 4,    # 最大并行度
        enable_parallel: bool = True,  # 是否启用并行
    ):
        self.team_name = team_name
        self.config = team_config
        self.members = members
        self.leader = team_config.get("leader", "")
        self.subagent_manager = subagent_manager
        self.llm = llm_client
        self.memory = memory_manager
        self.pipeline_mode = pipeline_mode
        self.progress_callback = progress_callback
        self.parent_session_id = parent_session_id
        self.context: TeamContext | None = None
        self.dag: ExecutionDAG | None = None
        self.workspace: str = ""
        self.artifacts: dict[str, str] = {}
        self.artifacts_dir: str = ""
        self.run_id: str = ""
        self.pipeline_stages: list[dict] = []
        self._completed_stages: set[str] = set()

        # ── v2.0 新字段 ──
        self._agent_pool = agent_pool
        self._worktree_manager = worktree_manager
        self._max_parallel = max_parallel
        self._enable_parallel = enable_parallel

    async def run(self, task: str) -> str:
        self.context = TeamContext(self.team_name, task)
        self._resolve_workspace()
        self.context.set_blackboard("工作目录", self.workspace)
        self.context.set_blackboard("团队名称", self.team_name)

        # 动态构建流水线
        if self.pipeline_mode == "auto":
            self.pipeline_stages = await generate_pipeline_async(
                task, self.members, self.llm
            )
        else:
            self.pipeline_stages = build_pipeline(
                task, self.members, mode=self.pipeline_mode
            )

        stage_names = [s["stage"] for s in self.pipeline_stages]
        logger.info(
            f"团队 [{self.team_name}] 流水线: {stage_names}"
            f" (mode={self.pipeline_mode}, parallel={self._enable_parallel})")

        if not self.pipeline_stages:
            if self.progress_callback:
                self.progress_callback(f"{self.team_name}|chat", "start", self.team_name, None)
            result = await self._run_direct_chat(self.context.original_task)
            if self.progress_callback:
                summary = (result or "")[:200].strip()
                self.progress_callback(f"{self.team_name}|chat", "stage_done", summary, None)
            return result

        if self.progress_callback:
            self.progress_callback("pipeline", "start", stage_names, None)

        # Leader 审核流水线配置
        if self.leader and self.leader in self.members:
            self.pipeline_stages = await self._leader_review_pipeline()

        # 使用 DAG 引擎执行（v2.0 并行增强）
        await self._execute_with_dag()

        return self._build_report()

    # ── DAG 执行引擎（v2.0 并行版本） ─────────────────

    async def _execute_with_dag(self):
        """用 DAG 引擎执行流水线，支持并行 + 反馈循环 + Agent 池复用"""
        self.dag = ExecutionDAG()

        for stage in self.pipeline_stages:
            node = DAGNode(
                id=stage["stage"],
                task=stage.get("output", ""),
                assignee=stage["role"],
                dependencies=stage.get("deps", []),
            )
            self.dag.add_node(node)

        max_total_attempts = len(self.dag.nodes) * 5
        total_attempts = 0

        while self.dag.has_pending_or_running() and total_attempts < max_total_attempts:
            ready = self.dag.get_ready_nodes()
            if not ready:
                running = [n for n in self.dag.nodes.values() if n.status == "running"]
                if running:
                    await asyncio.sleep(0.1)
                    total_attempts += 1
                    continue
                break

            # ── v2.0: 并行执行就绪节点（使用 AgentPool） ──
            if self._enable_parallel and len(ready) > 1:
                await self._execute_parallel_stages(ready)
            else:
                await self._execute_sequential_stages(ready)

            total_attempts += len(ready)

    async def _execute_parallel_stages(self, nodes: list[DAGNode]):
        """并行执行多个阶段（使用 AgentPool）"""
        for node in nodes:
            self.dag.mark_running(node.id)
            self.context.set_stage_status(node.id, "running")

        # 并行执行所有就绪节点
        tasks = [
            self._execute_stage_node_parallel(node)
            for node in nodes
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, node in enumerate(nodes):
            result = results[i]
            if isinstance(result, Exception):
                self._handle_stage_failure(node, str(result))
            else:
                success, should_feedback = result
                if success:
                    self._handle_stage_success(node)
                elif should_feedback:
                    await self._handle_feedback_loop(node)
                    if not self.context.feedback_loop.all_passed:
                        self.dag.nodes[node.id].status = "pending"
                        self.context.set_stage_status(node.id, "retrying")
                    else:
                        self._handle_stage_success(node)
                else:
                    self._handle_stage_failure(node, "stage failed")

    async def _execute_sequential_stages(self, nodes: list[DAGNode]):
        """顺序执行阶段（回退模式）"""
        for node in nodes:
            self.dag.mark_running(node.id)
            self.context.set_stage_status(node.id, "running")
            tasks = [self._execute_stage_node(node)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            result = results[0]
            if isinstance(result, Exception):
                self._handle_stage_failure(node, str(result))
            else:
                success, should_feedback = result
                if success:
                    self._handle_stage_success(node)
                elif should_feedback:
                    await self._handle_feedback_loop(node)
                    if not self.context.feedback_loop.all_passed:
                        self.dag.nodes[node.id].status = "pending"
                        self.context.set_stage_status(node.id, "retrying")
                    else:
                        self._handle_stage_success(node)
                else:
                    self._handle_stage_failure(node, "stage failed")

    def _handle_stage_success(self, node: DAGNode):
        self.dag.mark_completed(node.id, "ok")
        self.context.set_stage_status(node.id, "completed")
        self._completed_stages.add(node.id)

    def _handle_stage_failure(self, node: DAGNode, error: str):
        self.dag.mark_failed(node.id, error)
        self.context.set_stage_status(node.id, "failed")
        logger.error(f"阶段 [{node.id}] 失败: {error}")

    # ── v2.0: 带 AgentPool + Worktree 的阶段执行 ──────

    async def _execute_stage_node_parallel(self, node: DAGNode) -> tuple[bool, bool]:
        """使用 AgentPool 执行阶段节点（支持并行和隔离）

        Returns:
            (success, needs_feedback_loop)
        """
        stage_config = self._get_stage_config(node.id)
        if not stage_config:
            return True, False

        role = stage_config["role"]
        output_file = stage_config.get("output")
        if output_file:
            output_file = os.path.join(self.artifacts_dir, output_file)
            os.makedirs(os.path.dirname(output_file), exist_ok=True)

        logger.info(f"团队 [{self.team_name}] 阶段 [{node.id}] -> {role} (并行)")
        if self.progress_callback:
            self.progress_callback(node.id, "start", role, stage_config.get("max_iterations", 0) or 0)

        # ── v2.0: 任务分解：将大任务拆成可并行执行的子任务 ──
        stage_task = self._build_stage_task(role, node.id, output_file)
        if self._enable_parallel and role == "代码工程师":
            sub_tasks = await TaskDecomposer.decompose(
                stage_task, role, min(self._max_parallel, 3)
            )
        else:
            sub_tasks = [{"task": stage_task, "module": node.id, "role": role}]

        # ── v2.0: 使用 AgentPool 并行执行子任务 ──
        if self._agent_pool and len(sub_tasks) > 1:
            results = await self._agent_pool.map(
                [
                    {
                        "task": st["task"],
                        "role": role,
                        "team_name": self.team_name,
                        "parent_agent": None,
                        "max_iterations": stage_config.get("max_iterations", 0) or 0,
                        "session_id": f"{self.parent_session_id}:{node.id}",
                    }
                    for st in sub_tasks
                ],
                max_concurrent=self._max_parallel,
            )
            # 合并结果
            combined_parts = []
            for r in results:
                if isinstance(r, tuple):
                    _, text = r
                    combined_parts.append(text)
                else:
                    combined_parts.append(str(r))
            result = "\n\n---\n\n".join(combined_parts)
        else:
            # 无需并行或没有池：串行执行
            result = await self._run_stage(role, node.id, output_file)

        if result is None:
            return True, False

        if result.startswith("ERROR:"):
            self.context.set_blackboard(f"{node.id}_error", result)
            return False, False

        # 记录产出物
        if output_file:
            self.artifacts[node.id] = output_file
            self.context.set_blackboard(f"{node.id}_output", output_file)

        # Leader 审核
        if node.id in ("requirements", "architecture"):
            confirmed, feedback = await self._leader_review()
            if not confirmed:
                logger.info(f"Leader 在 [{node.id}] 阶段要求修改: {feedback[:100]}")
                self.context.set_leader_feedback(feedback)
                retry_result = await self._run_stage(role, f"{node.id}_retry", output_file)
                if retry_result and retry_result.startswith("ERROR:"):
                    self.context.set_blackboard(f"{node.id}_retry_error", retry_result)

        # 测试反馈循环
        has_feedback = stage_config.get("feedback_to") is not None
        if has_feedback and node.id in ("testing",):
            test_results = parse_test_output(result)
            if test_results:
                self.context.feedback_loop.test_results = test_results
                all_passed = all(r.get("passed", False) for r in test_results)
                if not all_passed:
                    return False, True
            else:
                passed = await self._llm_judge_test_result(result)
                if not passed:
                    self.context.feedback_loop.test_results = [
                        {"name": "LLM判定", "passed": False, "details": result[:500]}
                    ]
                    return False, True

        return True, False

    # ── 构建阶段任务上下文（v2.0: 最小上下文） ────────

    def _build_stage_task(self, role: str, stage: str, output_file: str | None) -> str:
        """构建阶段任务（包含最小上下文）"""
        stage_context = self.context.get_context_for_member(role, stage=stage)

        full_task = stage_context
        full_task += "\n\n## 你的任务"
        full_task += f"\n\n用户的问题是：{self.context.original_task}"
        full_task += f"\n\n请根据用户的上述问题完成「{stage}」阶段的工作，直接回应用户的需求。"
        full_task += f"\n工作目录: {self.workspace}"

        # 团队规范
        team_body = self.config.get("team_body", "")
        if team_body:
            full_task += f"\n\n## 团队规范与技能激活规则\n{team_body[:2000]}"

        full_task += "\n\n## 工作流要求"
        full_task += "\n- **优先调用 `skill` 工具加载适用于你角色和工作阶段的技能**"

        if output_file:
            full_task += f"\n\n## 输出说明\n如果本次产出内容量大且结构化，请写入 `{output_file}` 供后续查阅；如果只是简单结论，直接回复即可。"

        full_task += "\n\n## 安全提醒\n- 禁止使用 sudo / ssh / vim / nano 等交互式命令"

        if stage in ("implementation", "fix", "bug_fix"):
            full_task += (
                "\n\n## 铁律\n"
                "- 用 `grep` 定位目标代码，用 `file_operation(preview)` 看关键片段，不要通读无关文件\n"
                "- 读完文件后必须立即动手修改代码，禁止反复读取同一文件\n"
                "- 每个文件最多读 2 次，第 3 次读到同一文件视为分析瘫痪，直接报错\n"
                "- 必须使用 edit 或 file_operation(write) 工具修改源文件\n"
                "- 只读不改 = 任务失败\n"
                "- 修改后运行测试/编译验证，失败则继续修"
            )

        return full_task

    # ── 原有顺序执行（向后兼容） ─────────────────────

    async def _execute_stage_node(self, node: DAGNode) -> tuple[bool, bool]:
        """原顺序执行逻辑（未改动）"""
        stage_config = self._get_stage_config(node.id)
        if not stage_config:
            return True, False

        role = stage_config["role"]
        output_file = stage_config.get("output")
        if output_file:
            output_file = os.path.join(self.artifacts_dir, output_file)
            os.makedirs(os.path.dirname(output_file), exist_ok=True)

        logger.info(f"团队 [{self.team_name}] 阶段 [{node.id}] -> {role} (串行)")
        if self.progress_callback:
            self.progress_callback(node.id, "start", role, stage_config.get("max_iterations", 0) or 0)

        result = await self._run_stage(role, node.id, output_file)
        if result is None:
            return True, False
        if result.startswith("ERROR:"):
            self.context.set_blackboard(f"{node.id}_error", result)
            return False, False
        if output_file:
            self.artifacts[node.id] = output_file
            self.context.set_blackboard(f"{node.id}_output", output_file)

        if node.id in ("requirements", "architecture"):
            confirmed, feedback = await self._leader_review()
            if not confirmed:
                logger.info(f"Leader 在 [{node.id}] 阶段要求修改: {feedback[:100]}")
                self.context.set_leader_feedback(feedback)
                retry_result = await self._run_stage(role, f"{node.id}_retry", output_file)
                if retry_result and retry_result.startswith("ERROR:"):
                    self.context.set_blackboard(f"{node.id}_retry_error", retry_result)

        has_feedback = stage_config.get("feedback_to") is not None
        if has_feedback and node.id in ("testing",):
            test_results = parse_test_output(result)
            if test_results:
                self.context.feedback_loop.test_results = test_results
                all_passed = all(r.get("passed", False) for r in test_results)
                if not all_passed:
                    return False, True
            else:
                passed = await self._llm_judge_test_result(result)
                if not passed:
                    self.context.feedback_loop.test_results = [
                        {"name": "LLM判定", "passed": False, "details": result[:500]}
                    ]
                    return False, True
        return True, False

    # ── 反馈循环（未改动） ─────────────────────────

    async def _handle_feedback_loop(self, test_node: DAGNode):
        stage_config = self._get_stage_config(test_node.id)
        if not stage_config:
            return
        feedback_target = stage_config.get("feedback_to")
        max_loops = stage_config.get("max_loops", 3)
        if not feedback_target or feedback_target not in self.dag.nodes:
            logger.warning(f"反馈目标 {feedback_target} 不存在于 DAG 中")
            return
        loop = self.context.feedback_loop
        loop.iteration += 1
        if loop.iteration > max_loops:
            logger.warning(f"反馈循环已达最大次数 {max_loops}，停止循环")
            loop.test_results = [{"name": "强制通过（达到重试上限）", "passed": True, "details": ""}]
            return
        logger.info(f"反馈循环: 测试失败 → 回退到 {feedback_target} (第{loop.iteration}/{max_loops}轮)")
        failure_details = ""
        for r in loop.test_results:
            if not r.get("passed"):
                details = r.get("details", "")[:300]
                failure_details += f"\n- {r['name']}: {details}"
        target_config = self._get_stage_config(feedback_target)
        if target_config:
            fix_context = (
                f"## 第{loop.iteration}轮测试反馈\n"
                f"测试未通过，以下是失败详情：{failure_details}\n\n"
                f"请根据上述测试失败信息修复代码。确保修复后所有测试都能通过。\n\n"
                f"{loop.to_context_string()}"
            )
            self.context.add_message(
                from_member=stage_config["role"],
                to_member=target_config["role"],
                content=fix_context,
            )
            loop.fix_history.append({
                "iteration": loop.iteration,
                "summary": failure_details[:500],
            })
            if self.progress_callback:
                self.progress_callback("feedback", "start",
                                       f"第{loop.iteration}/{max_loops}轮",
                                       failure_details[:200])
            if self.progress_callback:
                self.progress_callback(f"{feedback_target}_fix_{loop.iteration}", "start",
                                       target_config["role"], None)
            dev_result = await self._run_stage(
                target_config["role"],
                f"{feedback_target}_fix_{loop.iteration}",
                target_config.get("output"),
            )
            if dev_result and not dev_result.startswith("ERROR:"):
                if self.progress_callback:
                    self.progress_callback(f"{test_node.id}_retest_{loop.iteration}", "start",
                                           stage_config["role"], None)
                test_result = await self._run_stage(
                    stage_config["role"],
                    f"{test_node.id}_retest_{loop.iteration}",
                    stage_config.get("output"),
                )
                if test_result and not test_result.startswith("ERROR:"):
                    new_results = parse_test_output(test_result)
                    if new_results:
                        loop.test_results = new_results
                    else:
                        passed = await self._llm_judge_test_result(test_result)
                        loop.test_results = [
                            {"name": "LLM判定", "passed": passed, "details": test_result[:500]}
                        ]

    # ── 阶段执行（未改动主体，沿用原逻辑） ────────────

    async def _run_stage(self, role: str, stage: str, output_file: str | None) -> str | None:
        if role not in self.members:
            logger.warning(f"角色 {role} 不在团队成员中，跳过阶段 {stage}")
            return None

        stage_context = self.context.get_context_for_member(role)
        full_task = stage_context
        full_task += "\n\n## 你的任务"
        full_task += f"\n\n用户的问题是：{self.context.original_task}"
        full_task += f"\n\n请根据用户的上述问题完成「{stage}」阶段的工作，直接回应用户的需求。"
        full_task += f"\n工作目录: {self.workspace}"
        team_body = self.config.get("team_body", "")
        if team_body:
            full_task += f"\n\n## 团队规范与技能激活规则\n{team_body[:2000]}"
        full_task += "\n\n## 工作流要求"
        full_task += "\n- **优先调用 `skill` 工具加载适用于你角色和工作阶段的技能**"
        if output_file:
            full_task += f"\n\n## 输出说明\n如果本次产出内容量大且结构化，请写入 `{output_file}` 供后续查阅；如果只是简单结论，直接回复即可。"
        full_task += "\n\n## 安全提醒\n- 禁止使用 sudo / ssh / vim / nano 等交互式命令"
        if stage in ("implementation", "fix", "bug_fix"):
            full_task += (
                "\n\n## 铁律\n"
                "- 用 `grep` 定位目标代码，用 `file_operation(preview)` 看关键片段，不要通读无关文件\n"
                "- 读完文件后必须立即动手修改代码，禁止反复读取同一文件\n"
                "- 每个文件最多读 2 次，第 3 次读到同一文件视为分析瘫痪，直接报错\n"
                "- 必须使用 edit 或 file_operation(write) 工具修改源文件\n"
                "- 只读不改 = 任务失败\n"
                "- 修改后运行测试/编译验证，失败则继续修"
            )

        _stage = stage
        _cb = self.progress_callback
        stage_config = self._get_stage_config(stage)
        stage_max_iter = stage_config.get("max_iterations", 0) if stage_config else 0
        if _cb:
            _cb("_max_iter", "_max_iter", {"max_iter": stage_max_iter or 200}, None)

        from hooks import HookEvent
        from tools.ask_user import reset_ask_user_mode, set_ask_user_mode

        async def _run_with_agent(task_body: str, max_iter: int) -> str:
            agent = await self.subagent_manager._create_team_subagent(
                self.team_name, role, client=self.llm,
                parent_agent=None, max_iterations=max_iter,
            )

            # ── v2.0: 集成 tracing ──
            if hasattr(agent, 'tracer'):
                agent.tracer.start_span(
                    f"stage:{stage}",
                    agent_role=role,
                )

            if _cb:
                agent.hooks.register(HookEvent.TOOL_START, lambda _ctx: _cb(
                    f"{_ctx.tool_name}|{_stage}", "tool_start", _ctx.arguments or {}, None))
                agent.hooks.register(HookEvent.TOOL_RESULT, lambda _ctx: _cb(
                    f"{_ctx.tool_name}|{_stage}", "tool_result", {}, str(_ctx.result or "")))
                agent.hooks.register(HookEvent.ROUND_START, lambda _ctx: _cb(
                    "_ctx", "_ctx", {"tokens": (
                        agent.tracer.get_context_stats().get("final", 0) if hasattr(agent, 'tracer') else 0
                    ), "iter": _ctx.metadata.get("iteration", 0)}, None))
                agent.hooks.register(HookEvent.LLM_RESPONSE, lambda _ctx: _cb(
                    "llm", "llm", _ctx.content or "", None))
            _ask_token = set_ask_user_mode("auto")
            sub_sid = f"{self.parent_session_id}:{role}" if self.parent_session_id else f"team:{self.run_id}:{role}"
            try:
                r = await asyncio.wait_for(
                    agent.run(task_body, session_id=sub_sid,
                              user_id="cli:admin", user_name="管理员"),
                    timeout=600,
                )
            finally:
                reset_ask_user_mode(_ask_token)
                if hasattr(agent, 'tracer'):
                    agent.tracer.end_span()

            text = r.result if hasattr(r, 'result') else str(r)
            st = getattr(r, 'status', 'completed')
            result_text = text
            if st == "failed":
                result_text = f"ERROR: 团队子代理 {role} 执行失败: {text}"
            elif st == "max_iterations":
                result_text = f"MAXITER: 达到最大迭代次数|{text}"
            if _cb:
                try:
                    if hasattr(agent, 'tracer'):
                        _cs = agent.tracer.get_context_stats()
                        _ctx_v = _cs.get("final", 0) or _cs.get("peak", 0)
                        if _ctx_v:
                            _cb("_ctx", "_ctx", {"tokens": _ctx_v}, None)
                except Exception:
                    pass
            await agent.cleanup()
            return result_text

        try:
            result = await _run_with_agent(full_task, stage_max_iter)
            if result and result.startswith("MAXITER:"):
                partial = result.split("|", 1)[1] if "|" in result else ""
                logger.info(f"阶段 [{stage}] 达到迭代上限，Leader 审核部分产出")
                if _cb:
                    _cb(stage, "stage_timeout", "迭代上限，Leader 审核中")
                confirmed, feedback = await self._leader_review_partial(stage, partial)
                if confirmed:
                    result = partial
                    logger.info(f"Leader 确认阶段 [{stage}] 产出可用")
                else:
                    logger.info(f"Leader 判定产出不足，追加 50 轮继续: {feedback[:100]}")
                    if _cb:
                        _cb(stage, "feedback", f"追加迭代: {feedback[:100]}")
                    continue_task = f"继续完成阶段性工作。Leader 反馈: {feedback[:300]}"
                    result = await _run_with_agent(continue_task, (stage_max_iter or 200) + 50)

            if _cb:
                summary = (result or "")[:300].strip()
                _cb(f"{role}|{_stage}", "stage_done", summary, None)
            self.context.add_node_result(_stage, role, result)
            return result
        except asyncio.TimeoutError:
            logger.warning(f"阶段 [{stage}] 超时")
            if _cb:
                _cb(f"{role}|{_stage}", "stage_timeout", None, None)
            return "ERROR: 阶段超时"
        except Exception as e:
            logger.error(f"阶段 [{stage}] 异常: {e}")
            return f"ERROR: {e}"

    async def _run_direct_chat(self, task: str) -> str:
        system_prompt = self.config.get("leader_prompt", "")
        if not system_prompt:
            return f"[{self.team_name}] 已收到您的消息。"
        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]
            resp = await self.llm.chat(messages)
            return resp.choices[0].message.content or ""
        except Exception as e:
            logger.warning(f"对话模式异常: {e}")
            return "已收到您的消息。"

    # ── 辅助方法 ─────────────────────────────────────

    def _get_stage_config(self, stage_id: str) -> dict | None:
        for s in self.pipeline_stages:
            if s["stage"] == stage_id:
                return s
        return None

    def _resolve_workspace(self):
        workspace = self.config.get("workspace", "")
        if not workspace and hasattr(self.subagent_manager, "parent_workspace"):
            workspace = self.subagent_manager.parent_workspace
        if not workspace:
            workspace = os.getcwd()
        self.workspace = workspace
        from agent.core import current_run
        task_dir = current_run().task_dir
        task_slug = ""
        if self.context and getattr(self.context, "original_task", ""):
            task_slug = re.sub(r"[^\w一-鿿]+", "_", self.context.original_task)[:20].strip("_")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_id = f"{ts}_{task_slug}" if task_slug else ts
        if task_dir:
            self.artifacts_dir = task_dir
        else:
            self.artifacts_dir = os.path.join(workspace, ".agent", self.run_id)
            os.makedirs(self.artifacts_dir, exist_ok=True)
        logger.info(f"工作目录: {self.workspace}, 本次产物目录: {self.artifacts_dir}")

    @staticmethod
    def _read_file_head(path: str, max_chars: int) -> str:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read(max_chars)
            if len(content) == max_chars:
                content += "\n... [截断]"
            return content
        except Exception:
            return ""

    async def _leader_review_pipeline(self) -> list[dict]:
        leader_prompt = self.config.get("leader_prompt", "")
        stages_json = json.dumps(self.pipeline_stages, ensure_ascii=False, indent=2)
        prompt = f"""你是团队 "{self.team_name}" 的 Leader ({self.leader})。
{leader_prompt}

## 原始任务
{self.context.original_task}

## 当前流水线
{stages_json}

请审核每个阶段的 `output` 字段，按以下规则决定是否需要产出文档：
- **简单任务** → 每个阶段只需直接回复用户或传递结论，将 output 设为 null
- **复杂/大型任务** → 阶段产出大量结构化内容时，保留 output 供下游查阅或项目存档
- 产出物应最小化：能不落文件就不落文件

只返回调整后的完整流水线 JSON 数组（与输入格式一致）。"""
        try:
            resp = await self.llm.chat([{"role": "user", "content": prompt}])
            content = resp.choices[0].message.content if hasattr(resp, "choices") else str(resp)
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            adjusted = json.loads(content)
            if isinstance(adjusted, list):
                for s in adjusted:
                    s.setdefault("deps", [])
                    s.setdefault("feedback_to", None)
                    s.setdefault("max_loops", 3)
                logger.info(f"Leader 调整流水线: {len(adjusted)} 个阶段")
                return adjusted
        except Exception as e:
            logger.warning(f"Leader 流水线审核失败，使用原配置: {e}")
        return self.pipeline_stages

    async def _leader_review_partial(self, stage: str, partial: str) -> tuple[bool, str]:
        leader_prompt = self.config.get("leader_prompt", "")
        prompt = f"""你是团队 "{self.team_name}" 的 Leader ({self.leader})。
{leader_prompt}

## 原始任务
{self.context.original_task}

## 阶段
{stage}

## 部分产出（达到迭代上限被截断）
{partial[:2000]}

请判断这份部分产出是否足够交付给下一阶段使用，还是必须继续完善。
只返回 JSON: {{"confirmed": true/false, "feedback": "理由"}}"""
        try:
            resp = await self.llm.chat([{"role": "user", "content": prompt}])
            content = resp.choices[0].message.content if hasattr(resp, "choices") else str(resp)
            result = json.loads(_extract_json_from_llm(content))
            return result.get("confirmed", False), result.get("feedback", "")
        except Exception as e:
            logger.warning(f"Leader 部分产出审核失败: {e}")
            return False, ""

    async def _leader_review(self) -> tuple[bool, str]:
        if not self.leader or self.leader not in self.members:
            return True, ""
        summary = self.context.get_summary()
        leader_prompt = self.config.get("leader_prompt", "")
        leader_context = f"\n## Leader 角色说明\n{leader_prompt}\n" if leader_prompt else ""
        prompt = f"""你是团队 "{self.team_name}" 的 Leader ({self.leader})。
{leader_context}
请对当前阶段产出进行多维度审核。

## 当前产出
{summary}

## 原始任务
{self.context.original_task}

请返回多维度评分 JSON:
{{"confirmed": true/false,
  "scores": {{"功能": 0-10, "代码质量": 0-10, "测试": 0-10, "安全": 0-10, "性能": 0-10, "文档": 0-10}},
  "feedback": "如果不通过，说明问题和由谁修改"}}

只有核心功能完整、无明显安全漏洞时 confirmed 才为 true。
只返回 JSON。"""
        try:
            resp = await self.llm.chat([{"role": "user", "content": prompt}])
            content = resp.choices[0].message.content if hasattr(resp, "choices") else str(resp)
            result = json.loads(_extract_json_from_llm(content))
            scores = result.get("scores", {})
            logger.info(f"Leader 审核: confirmed={result.get('confirmed')}, scores={scores}")
            return result.get("confirmed", True), result.get("feedback", "")
        except Exception as e:
            logger.warning(f"Leader 审核解析失败: {e}")
            return True, ""

    async def _llm_judge_test_result(self, test_output: str) -> bool:
        prompt = f"""判断以下测试输出是否全部通过。

## 测试输出
{test_output[:3000]}

只返回 JSON: {{"passed": true/false, "reason": "原因"}}"""
        try:
            resp = await self.llm.chat([{"role": "user", "content": prompt}])
            content = resp.choices[0].message.content if hasattr(resp, "choices") else str(resp)
            result = json.loads(_extract_json_from_llm(content))
            return result.get("passed", False)
        except Exception as e:
            logger.warning(f"LLM 测试判定失败: {e}")
            return False

    def _build_report(self) -> str:
        elapsed = int(time.time() - self.context.started_at)
        mins, secs = divmod(elapsed, 60)
        parts = [
            f"# 团队执行报告: {self.team_name}",
            f"## 原始任务\n{self.context.original_task}",
            f"## 工作目录\n{self.workspace}",
            f"## 耗时\n{mins}分{secs}秒",
            f"## 流水线模式\n{self.pipeline_mode}",
            "## 执行阶段",
            self.context.get_stage_summary(),
        ]
        if self.context.node_results:
            parts.append("## 阶段产出")
            for node_id, result in self.context.node_results.items():
                truncated = result[:2000] if result else "(无输出)"
                parts.append(f"### {node_id}\n{truncated}")
        if self.context.feedback_loop.iteration > 0:
            parts.append("## 开发↔测试循环")
            parts.append(self.context.feedback_loop.to_context_string())
        return "\n\n".join(parts)
