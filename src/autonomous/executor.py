import logging

from autonomous.goal import Plan, PlanStep

logger = logging.getLogger("agent.autonomous.executor")


class Executor:
    def __init__(self, agent, reporter=None):
        self.agent = agent
        self.reporter = reporter

    async def execute_plan(self, plan: Plan) -> list[PlanStep]:
        executed = []
        sorted_steps = sorted(plan.steps, key=lambda s: s.order)

        for step in sorted_steps:
            if step.status == "completed":
                continue

            step.status = "running"

            try:
                result = await self.agent.run(step.task_description)
                step.status = result.status
                step.result = result.result
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
