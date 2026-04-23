import asyncio
import contextlib
import logging

from autonomous.eventbus import EventBus
from autonomous.goal import Goal, GoalManager, PlanStep

logger = logging.getLogger("agent.autonomous.loop")


class AutonomousLoop:
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
        self.shutdown_event = shutdown_event or asyncio.Event()
        self._discovery_interval = 300
        self._discovery_task: asyncio.Task | None = None

    async def run(self):
        self._discovery_task = asyncio.create_task(self._self_discovery_loop())
        try:
            while not self.shutdown_event.is_set():
                try:
                    await self._process_next_goal(timeout=30)
                except Exception:
                    logger.exception("主循环处理目标异常")
                    await asyncio.sleep(5)
        finally:
            if self._discovery_task:
                self._discovery_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._discovery_task

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
                    logger.warning(f"目标 [{goal.id}] 规划结果为空，直接执行整个目标")
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
            logger.info(f"目标 [{goal.id}] 失败，但存在部分结果，推送最佳结果")
            await self.reporter.report_failure_with_partial(goal, best_result)
        else:
            await self.reporter.report_failure(goal, type("V", (), {
                "summary": "所有尝试均失败", "feedback": "请稍后重试或联系管理员"
            })())

        return goal

    def _collect_best_result(self, steps: list) -> str:
        completed = [s for s in steps if s.status == "completed" and s.result]
        if completed:
            last = completed[-1]
            return f"步骤「{last.task_description}」的结果:\n{last.result}"
        all_with_result = [s for s in steps if s.result]
        if all_with_result:
            last = all_with_result[-1]
            return f"步骤「{last.task_description}」的部分结果:\n{last.result}"
        return ""

    async def _self_discovery_loop(self):
        while not self.shutdown_event.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self.shutdown_event.wait(), timeout=self._discovery_interval
                )
            if self.shutdown_event.is_set():
                break
            try:
                await self.perceiver.self_discovery_check()
            except Exception:
                logger.exception("自发现检查异常")
