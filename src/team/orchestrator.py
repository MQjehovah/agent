import asyncio
import json
import logging
import os
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
        self.context: TeamContext | None = None
        self.dag: ExecutionDAG | None = None
        self.workspace: str = ""
        self.artifacts: dict[str, str] = {}
        self.pipeline_stages: list[dict] = []
        self._completed_stages: set[str] = set()

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
            f" (mode={self.pipeline_mode})")
        if self.progress_callback:
            self.progress_callback("pipeline", "start", stage_names)

        # 使用 DAG 引擎执行
        await self._execute_with_dag()

        return self._build_report()

    # ── DAG 执行引擎 ─────────────────────────────────────

    async def _execute_with_dag(self):
        """用 DAG 引擎执行流水线，支持并行 + 反馈循环"""
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

            # 并行执行就绪节点
            tasks = []
            for node in ready:
                self.dag.mark_running(node.id)
                self.context.set_stage_status(node.id, "running")
                tasks.append(self._execute_stage_node(node))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for i, node in enumerate(ready):
                result = results[i]
                if isinstance(result, Exception):
                    self.dag.mark_failed(node.id, str(result))
                    self.context.set_stage_status(node.id, "failed")
                    logger.error(f"阶段 [{node.id}] 异常: {result}")
                else:
                    success, should_feedback = result
                    if success:
                        self.dag.mark_completed(node.id, "ok")
                        self.context.set_stage_status(node.id, "completed")
                        self._completed_stages.add(node.id)
                    elif should_feedback:
                        await self._handle_feedback_loop(node)
                        if not self.context.feedback_loop.all_passed:
                            self.dag.nodes[node.id].status = "pending"
                            self.context.set_stage_status(node.id, "retrying")
                        else:
                            self.dag.mark_completed(node.id, "ok")
                            self.context.set_stage_status(node.id, "completed")
                            self._completed_stages.add(node.id)
                    else:
                        self.dag.mark_failed(node.id, "stage failed")
                        self.context.set_stage_status(node.id, "failed")

            total_attempts += len(ready)

    async def _execute_stage_node(self, node: DAGNode) -> tuple[bool, bool]:
        """执行单个阶段节点

        Returns:
            (success, needs_feedback_loop)
        """
        stage_config = self._get_stage_config(node.id)
        if not stage_config:
            return True, False

        role = stage_config["role"]
        output_file = stage_config.get("output")

        logger.info(f"团队 [{self.team_name}] 阶段 [{node.id}] -> {role}")
        if self.progress_callback:
            self.progress_callback(node.id, "start", role)

        result = await self._run_stage(role, node.id, output_file)
        if result is None:
            return True, False

        if result.startswith("ERROR:"):
            self.context.set_blackboard(f"{node.id}_error", result)
            return False, False

        # 记录产出物路径（不读内容）
        if output_file:
            artifact_path = os.path.join(self.workspace, output_file)
            self.artifacts[node.id] = artifact_path
            self.context.set_blackboard(f"{node.id}_output", artifact_path)

        # Leader 审核（关键阶段）
        if node.id in ("requirements", "architecture"):
            confirmed, feedback = await self._leader_review()
            if not confirmed:
                logger.info(f"Leader 在 [{node.id}] 阶段要求修改: {feedback[:100]}")
                self.context.set_leader_feedback(feedback)
                retry_result = await self._run_stage(role, f"{node.id}_retry", output_file)
                if retry_result and retry_result.startswith("ERROR:"):
                    self.context.set_blackboard(f"{node.id}_retry_error", retry_result)

        # 检测是否需要反馈循环（测试阶段）
        has_feedback = stage_config.get("feedback_to") is not None
        if has_feedback and node.id in ("testing",):
            test_results = parse_test_output(result)
            if test_results:
                self.context.feedback_loop.test_results = test_results
                all_passed = all(r.get("passed", False) for r in test_results)
                if not all_passed:
                    return False, True
            else:
                # 无法解析测试结果，用 LLM 判断
                passed = await self._llm_judge_test_result(result)
                if not passed:
                    self.context.feedback_loop.test_results = [
                        {"name": "LLM判定", "passed": False, "details": result[:500]}
                    ]
                    return False, True

        return True, False

    # ── 反馈循环 ──────────────────────────────────────────

    async def _handle_feedback_loop(self, test_node: DAGNode):
        """处理测试→开发反馈循环"""
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
            logger.warning(
                f"反馈循环已达最大次数 {max_loops}，停止循环")
            loop.test_results = [{"name": "强制通过（达到重试上限）", "passed": True, "details": ""}]
            return

        logger.info(
            f"反馈循环: 测试失败 → 回退到 {feedback_target}"
            f" (第{loop.iteration}/{max_loops}轮)")

        # 收集失败详情（精简）
        failure_details = ""
        for r in loop.test_results:
            if not r.get("passed"):
                details = r.get("details", "")[:300]
                failure_details += f"\n- {r['name']}: {details}"

        # 向开发工程师发送修复任务
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

            # 重新执行开发阶段
            dev_result = await self._run_stage(
                target_config["role"],
                f"{feedback_target}_fix_{loop.iteration}",
                target_config.get("output"),
            )

            if dev_result and not dev_result.startswith("ERROR:"):
                # 重新执行测试
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

    # ── 阶段执行 ──────────────────────────────────────────

    async def _run_stage(self, role: str, stage: str, output_file: str | None) -> str | None:
        if role not in self.members:
            logger.warning(f"角色 {role} 不在团队成员中，跳过阶段 {stage}")
            return None

        stage_context = self.context.get_context_for_member(role)

        full_task = stage_context
        full_task += f"\n\n## 你的任务\n根据你的角色职责完成「{stage}」阶段的工作。"
        full_task += "\n\n## 角色技能\n你拥有本角色专属技能，可通过 execute_skill 工具调用。"
        full_task += "\n\n## 安全提醒\n- 禁止使用 sudo / ssh / vim / nano 等交互式命令"
        if output_file:
            full_task += f"\n将输出写入 `{output_file}`。"
        full_task += f"\n工作目录: {self.workspace}"

        _stage = stage
        _cb = self.progress_callback
        try:
            result = await self.subagent_manager.run_team_agent(
                team_name=self.team_name,
                member_name=role,
                task=full_task,
                tool_callback=lambda evt, name, args, res: (
                    _cb(f"tool_{evt}", f"{name}|{_stage}", args, res)
                ) if _cb else None,
            )
            self.context.add_node_result(_stage, role, result)
            return result
        except Exception as e:
            logger.error(f"阶段 [{stage}] 异常: {e}")
            return f"ERROR: {e}"

    # ── 辅助方法 ──────────────────────────────────────────

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
        logger.info(f"工作目录: {self.workspace}")

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
        """用 LLM 判断测试是否通过（当无法自动解析时）"""
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
        import time as _time

        elapsed = int(_time.time() - self.context.started_at)
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

        if self.context.feedback_loop.iteration > 0:
            parts.append("## 开发↔测试循环")
            parts.append(self.context.feedback_loop.to_context_string())

        parts.append(f"\n## 产出物\n{self.workspace}/")
        if os.path.exists(self.workspace):
            for f in os.listdir(self.workspace):
                if os.path.isfile(os.path.join(self.workspace, f)):
                    parts.append(f"  - {f}")

        return "\n\n".join(parts)
