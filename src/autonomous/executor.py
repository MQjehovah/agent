import logging

from autonomous.goal import Plan, PlanStep

logger = logging.getLogger("agent.autonomous.executor")

MAX_PREVIOUS_RESULTS_CHARS = 8000


class Executor:
    def __init__(self, agent, reporter=None):
        self.agent = agent
        self.reporter = reporter

    async def execute_plan(self, plan: Plan) -> list[PlanStep]:
        executed = []
        sorted_steps = sorted(plan.steps, key=lambda s: s.order)
        previous_results: list[str] = []
        session_id = plan.goal_id

        for step in sorted_steps:
            if step.status == "completed":
                continue

            step.status = "running"

            task_with_context = step.task_description
            if previous_results:
                self._trim_previous_results(previous_results)
                context_block = "\n\n---\n以下是前序步骤的执行结果，请在此基础上继续：\n" + "\n".join(previous_results)
                task_with_context = step.task_description + context_block

            try:
                result = await self.agent.run(task_with_context, session_id=session_id)
                step.status = result.status
                step.result = result.result
                if step.result:
                    result_preview = step.result[:3000]
                    previous_results.append(
                        f"【步骤{step.order}】{step.task_description}\n结果: {result_preview}"
                    )
            except Exception as e:
                step.status = "failed"
                step.result = str(e)
                if self.reporter:
                    await self.reporter.report_progress(step, step)
                executed.append(step)
                break

            if self.reporter:
                await self.reporter.report_progress(step, result)

            if step.requires_confirmation and step.status == "completed" and self.reporter:
                confirmed = await self.reporter.ask_confirmation(step)
                if not confirmed:
                    step.status = "rejected"
                    executed.append(step)
                    break

            executed.append(step)

            if step.status == "failed":
                break

        return executed

    @staticmethod
    def _trim_previous_results(results: list[str]):
        """保证 previous_results 总字符数不超过预算"""
        total = 0
        for r in results:
            total += len(r)
        while total > MAX_PREVIOUS_RESULTS_CHARS and len(results) > 1:
            removed = results.pop(0)
            total -= len(removed)
        if total > MAX_PREVIOUS_RESULTS_CHARS and results:
            r = results[0]
            results[0] = r[:MAX_PREVIOUS_RESULTS_CHARS - 20] + "\n... [截断]"
