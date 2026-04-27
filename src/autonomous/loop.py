import asyncio
import contextlib
import logging
import os

from autonomous.eventbus import EventBus
from autonomous.goal import Goal, GoalManager, PlanStep
from autonomous.panel import TaskPanel, PanelTask

logger = logging.getLogger("agent.autonomous.loop")


class AutonomousLoop:
    PANEL_POLL_INTERVAL = 60  # 面板轮询间隔: 1 分钟

    def __init__(
        self,
        event_bus: EventBus,
        agent,
        goal_manager: GoalManager,
        planner,
        executor,
        verifier,
        reporter,
        perceiver,
        panel: TaskPanel,
        shutdown_event: asyncio.Event | None = None,
    ):
        self.event_bus = event_bus
        self.agent = agent
        self.goal_manager = goal_manager
        self.planner = planner
        self.executor = executor
        self.verifier = verifier
        self.reporter = reporter
        self.perceiver = perceiver
        self.panel = panel
        self.shutdown_event = shutdown_event or asyncio.Event()
        self._active_tasks: list[asyncio.Task] = []
        self._agent_panels: dict[str, TaskPanel] = {}  # agent_name → panel

    async def run(self):
        # 1. 启动时生成所有 agent 的面板任务
        await self._generate_all_panels()

        # 2. 启动面板执行循环
        self._active_tasks = [
            asyncio.create_task(self._panel_loop()),
        ]
        try:
            while not self.shutdown_event.is_set():
                try:
                    await self._process_next_goal(timeout=30)
                except Exception:
                    logger.exception("主循环处理目标异常")
                    await asyncio.sleep(5)
        finally:
            for t in self._active_tasks:
                t.cancel()
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
            self._active_tasks.clear()

    # ================================================================
    #  启动时一次性生成所有 agent 的面板任务
    # ================================================================

    async def _generate_all_panels(self):
        """为根 agent 和所有子 agent 生成任务面板"""
        tool_summary = getattr(self.agent, "_get_tool_summary", lambda: "")()
        subagent_summary = ""
        if self.agent.subagent_manager:
            subagent_summary = self.agent.subagent_manager.get_subagent_prompt()

        # 根 agent
        logger.info("为根 agent [%s] 生成任务面板...", getattr(self.agent, "name", "?"))
        if self.panel.is_empty():
            await self.perceiver.generate_panel_tasks(
                self.agent, tool_summary, subagent_summary, self.panel
            )

        # 子 agent
        if self.agent.subagent_manager:
            for name in self.agent.subagent_manager.list_templates():
                await self._ensure_subagent_panel(name)

    async def _ensure_subagent_panel(self, name: str):
        """确保子 agent 有任务面板，没有就生成"""
        if name in self._agent_panels:
            return
        subagent = await self._create_subagent_for_panel(name)
        if subagent is None:
            return
        workspace = subagent.workspace
        panel_path = os.path.join(workspace, "task_panel.json")
        sub_panel = TaskPanel(panel_path)
        self._agent_panels[name] = sub_panel

        if sub_panel.is_empty():
            st = getattr(subagent, "_get_tool_summary", lambda: "")()
            subagent_summary = ""
            if self.agent.subagent_manager:
                subagent_summary = self.agent.subagent_manager.get_subagent_prompt()
            logger.info("为子 agent [%s] 生成任务面板...", name)
            await self.perceiver.generate_panel_tasks(
                subagent, st, subagent_summary, sub_panel
            )
        try:
            await subagent.cleanup()
        except Exception:
            pass

    async def _create_subagent_for_panel(self, name: str):
        """创建一个临时子 agent 实例用于读取 PROMPT.md"""
        from agent import Agent
        template = self.agent.subagent_manager.get_template(name)
        if not template:
            return None
        workspace = template.get("workspace")
        if not workspace or not os.path.isdir(workspace):
            return None
        agent = Agent(
            workspace=workspace,
            client=self.agent.client,
            parent_agent=self.agent,
        )
        await agent.initialize()
        return agent

    # ================================================================
    #  事件驱动入口
    # ================================================================

    async def _process_next_goal(self, timeout: float = 30.0) -> Goal | None:
        try:
            event = await self.event_bus.get(timeout=timeout)
        except asyncio.TimeoutError:
            return None

        goal_info = await self.perceiver.resolve_goal_from_event(
            event.type, event.payload
        )
        if goal_info is None:
            return None

        goal = self.goal_manager.create_goal(
            title=goal_info.get("title", ""),
            description=goal_info.get("description", ""),
            source=event.source,
            priority=event.priority,
        )
        return await self._execute_goal_loop(goal)

    async def _execute_goal_loop(self, goal: Goal) -> Goal:
        feedback = ""
        executed_steps = []
        for attempt in range(goal.max_retries):
            try:
                if attempt == 0:
                    plan = await self.planner.plan(goal)
                else:
                    completed_steps = [
                        s for s in executed_steps if s.status == "completed"
                    ]
                    failed_steps = [
                        s for s in executed_steps if s.status != "completed"
                    ]
                    if not failed_steps and not feedback:
                        break
                    plan = await self.planner.replan(
                        goal, feedback, completed_steps, failed_steps
                    )

                if not plan.steps:
                    plan.steps = [PlanStep(
                        task_description=goal.description, order=0
                    )]

                self.goal_manager.save_plan(goal.id, plan)
                self.goal_manager.update_status(goal.id, "executing")
                executed_steps = await self.executor.execute_plan(plan)
                self.goal_manager.update_status(goal.id, "verifying")
                verification = await self.verifier.verify(goal, executed_steps)

                if verification.passed:
                    self.goal_manager.update_status(goal.id, "completed")
                    await self.reporter.report_success(goal)
                    goal.status = "completed"
                    return goal

                await self.reporter.report_failure(goal, verification)
                feedback = verification.feedback
                self.goal_manager.increment_retry(goal.id)
            except Exception:
                logger.exception("执行目标循环异常 (attempt=%d)", attempt)
                feedback = "执行异常"
                self.goal_manager.increment_retry(goal.id)

        self.goal_manager.update_status(goal.id, "failed")
        goal.status = "failed"
        best_result = self._collect_best_result(executed_steps)
        if best_result:
            await self.reporter.report_failure_with_partial(goal, best_result)
        else:
            await self.reporter.report_failure(goal, type("V", (), {
                "summary": "所有尝试均失败", "feedback": "请稍后重试"
            })())
        return goal

    def _collect_best_result(self, steps: list) -> str:
        completed = [s for s in steps if s.status == "completed" and s.result]
        if completed:
            return f"步骤「{completed[-1].task_description}」的结果:\n{completed[-1].result}"
        all_with_result = [s for s in steps if s.result]
        if all_with_result:
            return f"步骤「{all_with_result[-1].task_description}」的部分结果:\n{all_with_result[-1].result}"
        return ""

    # ================================================================
    #  面板执行循环（轮询所有 agent 的面板）
    # ================================================================

    async def _panel_loop(self):
        await self._poll_and_execute()
        while not self.shutdown_event.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self.shutdown_event.wait(), timeout=self.PANEL_POLL_INTERVAL
                )
            if self.shutdown_event.is_set():
                break
            try:
                await self._poll_and_execute()
            except Exception:
                logger.exception("面板循环异常")

    async def _poll_and_execute(self):
        if self.event_bus.size() > 0:
            return

        # 收集所有面板的到期任务
        all_pending: list[tuple[str | None, PanelTask]] = []
        for t in self.panel.get_pending():
            all_pending.append((None, t))
        for name, sub_panel in self._agent_panels.items():
            for t in sub_panel.get_pending():
                all_pending.append((name, t))

        all_pending.sort(key=lambda x: (x[1].priority, x[1].created_at))
        if all_pending:
            logger.info("面板中有 %d 个任务待执行", len(all_pending))

        for agent_name, task in all_pending:
            if self.shutdown_event.is_set() or self.event_bus.size() > 0:
                break
            panel = self._agent_panels.get(agent_name, self.panel) if agent_name else self.panel
            logger.info("执行面板任务: [%s][%s] %s", agent_name or "root", task.source, task.title)
            panel.mark_active(task.id)
            try:
                goal = self.goal_manager.create_goal(
                    title=task.title,
                    description=task.description,
                    source=f"panel:{agent_name or 'root'}:{task.source}",
                    priority=task.priority,
                )
                await self._execute_goal_loop(goal)
            except Exception:
                logger.exception("面板任务执行异常: %s", task.title)
            finally:
                if task.interval is not None:
                    panel.mark_pending(task.id)
                else:
                    panel.mark_completed(task.id)
