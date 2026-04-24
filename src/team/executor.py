import logging

from src.memory.manager import MemoryManager
from src.subagent_manager import SubagentManager
from src.team.errors import classify_failure
from src.team.pipeline import Pipeline, PipelineStage

logger = logging.getLogger("agent.team.executor")


class TeamExecutor:
    """Executes a pipeline by delegating stages to team member sub-agents."""

    def __init__(
        self,
        workspace: str,
        subagent_manager: SubagentManager,
        memory_manager: MemoryManager,
    ):
        self.workspace = workspace
        self.subagent_manager = subagent_manager
        self.memory = memory_manager
        self.context: dict[str, str] = {}

    async def execute_pipeline(self, pipeline: Pipeline, user_request: str) -> bool:
        """Execute all stages in the pipeline sequentially.

        Returns True if all stages completed successfully.
        """
        while not pipeline.is_complete():
            stage = pipeline.current_stage
            logger.info(f"Executing stage: {stage.name} (agent={stage.agent})")

            task = self._build_task_description(stage, user_request)
            try:
                result = await self.subagent_manager.run_team_agent(
                    team_name="AI开发团队",
                    member_name=stage.agent,
                    task=task,
                )
                stage.record_attempt(success=True, result=result)
                self.context[stage.name] = result
                self._save_to_memory(stage, result)
                pipeline.advance()
            except Exception as e:
                stage.record_attempt(success=False, result=str(e))
                logger.warning(f"Stage {stage.name} failed: {e}")

                action = classify_failure(str(e))
                fallback_stage = self._determine_fallback(action.stage)
                if fallback_stage:
                    logger.info(
                        f"Fallback from {stage.name} to {fallback_stage} "
                        f"(attempt {stage.attempt_count}/{stage.retry_policy.max_retries})"
                    )
                    pipeline.reset_to(fallback_stage)
                    continue

                if not stage.should_retry():
                    logger.error(
                        f"Stage {stage.name} exhausted retries "
                        f"({stage.attempt_count}/{stage.retry_policy.max_retries})"
                    )
                    return False

        return True

    def _build_task_description(self, stage: PipelineStage, user_request: str) -> str:
        """Build the task description for a stage, including upstream results."""
        parts = [f"## Project Goal\n{user_request}"]
        for name, output in self.context.items():
            if name == stage.name:
                break
            parts.append(f"## {name} Stage Output\n{output[:3000]}")
        return "\n\n".join(parts)

    def _determine_fallback(self, error_stage: str) -> str | None:
        """Determine which stage to fall back to given the error type."""
        mapping = {
            "test": "code",
        }
        return mapping.get(error_stage)

    def _save_to_memory(self, stage: PipelineStage, result: str):
        """Save stage result to shared memory."""
        try:
            self.memory.share_knowledge(
                from_agent=stage.agent,
                knowledge=f"[{stage.name}] {result[:500]}",
            )
        except Exception as e:
            logger.warning(f"Failed to save to shared memory: {e}")
