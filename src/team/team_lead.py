import json
import logging

from src.llm import LLMClient
from src.memory.manager import MemoryManager
from src.subagent_manager import SubagentManager
from src.team.executor import TeamExecutor
from src.team.pipeline import Pipeline, PipelineStage

logger = logging.getLogger("agent.team.lead")


STAGE_AGENT_MAP = {
    "research": "算法研究员",
    "arch": "软件架构师",
    "code": "代码工程师",
    "test": "测试工程师",
    "devops": "DevOps工程师",
    "docs": "文档专员",
}

DEFAULT_PIPELINE = ["arch", "code", "test", "devops"]


class TeamLeadAgent:
    """Team Lead agent that plans and orchestrates a development pipeline."""

    def __init__(
        self,
        workspace: str,
        llm_client: LLMClient,
        executor: TeamExecutor = None,
        subagent_manager: SubagentManager = None,
        memory_manager: MemoryManager = None,
    ):
        self.workspace = workspace
        self.llm = llm_client
        self.executor = executor or TeamExecutor(
            workspace=workspace,
            subagent_manager=subagent_manager,
            memory_manager=memory_manager,
        )

    async def run(self, user_request: str) -> str:
        """Run the full team workflow: plan execute report."""
        logger.info(f"Team Lead received request: {user_request[:100]}")

        pipeline = await self._plan_pipeline(user_request)
        if not pipeline:
            return self._build_report(user_request, success=False,
                                      error="Failed to create pipeline plan")

        if len(pipeline.stages) == 0:
            return self._build_report(user_request, success=False,
                                      error="Pipeline has no stages")

        stage_names = [s.name for s in pipeline.stages]
        logger.info(f"Pipeline planned: {stage_names}")

        success = await self.executor.execute_pipeline(pipeline, user_request)

        if success:
            logger.info("Pipeline completed successfully")
        else:
            logger.warning("Pipeline completed with failures")

        return self._build_report(
            user_request=user_request,
            success=success,
            stages=stage_names,
            context=self.executor.context,
        )

    async def _plan_pipeline(self, user_request: str) -> Pipeline | None:
        """Use LLM to plan the pipeline stages based on the user request."""
        prompt = (
            "You are a software development team lead. Based on the project goal below, "
            "choose the appropriate development stages and return them as a JSON array.\n\n"
            "Available stages:\n"
            "- research: Algorithm research and selection\n"
            "- arch: Software architecture design\n"
            "- code: Coding and compilation\n"
            "- test: Unit and integration testing\n"
            "- devops: Environment setup and deployment\n"
            "- docs: Documentation (optional)\n\n"
            "Rules:\n"
            "- Always include arch, code, test\n"
            "- Include research if the project involves algorithm selection\n"
            "- Include devops if deployment is needed\n"
            "- Include docs only if explicitly requested or if the project is large\n"
            "- Order them logically\n\n"
            "Return ONLY a JSON array, no other text.\n"
            'Example: ["research", "arch", "code", "test", "devops"]\n\n'
            f"Project goal: {user_request}"
        )

        try:
            messages = [{"role": "user", "content": prompt}]
            resp = await self.llm.chat(messages)
            content = resp.choices[0].message.content if hasattr(resp, "choices") else str(resp)
            stages = json.loads(content.strip())
            if not isinstance(stages, list):
                raise ValueError("Not a list")
        except Exception:
            logger.warning("Failed to parse pipeline plan, using default")
            stages = DEFAULT_PIPELINE.copy()

        valid_stages = [s for s in stages if s in STAGE_AGENT_MAP]
        if not valid_stages:
            valid_stages = DEFAULT_PIPELINE.copy()

        return Pipeline(stages=[
            PipelineStage(name=s, agent=STAGE_AGENT_MAP[s])
            for s in valid_stages
        ])

    @staticmethod
    def _build_report(
        user_request: str,
        success: bool,
        stages: list[str] = None,
        context: dict[str, str] = None,
        error: str = "",
    ) -> str:
        """Build a summary report for the user."""
        parts = [f"# Project Report\n## Goal\n{user_request}\n"]

        if success:
            parts.append("## Status\nAll stages completed successfully.\n")
        else:
            parts.append("## Status\nPipeline completed with failures.\n")
            if error:
                parts.append(f"### Error\n{error}\n")

        if stages:
            parts.append("## Pipeline\n")
            status_icons = []
            if context:
                for s in stages:
                    status_icons.append(f"- {s}")
            else:
                status_icons = [f"- {s}" for s in stages]
            parts.append("\n".join(status_icons))
            parts.append("")

        if context:
            parts.append("## Stage Outputs\n")
            for stage_name, output in context.items():
                parts.append(f"### {stage_name}\n{output[:500]}\n")

        return "\n".join(parts)
