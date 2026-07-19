"""
Agent 循环入口 — 向后兼容 re-export

ReAct 循环已迁移到 agent/reactor.py（run_impl, run_impl_reflective, think）。
team_run_impl 保留在此。
"""
import logging

from agent.context import AgentResult
from agent.reactor import run_impl, run_impl_reflective, think, think_stream
from agent.reactor import execute_tool_calls_parallel, execute_tool_calls_parallel_reflective
from team.worktree import WorktreeManager
from team.orchestrator import TeamOrchestrator

logger = logging.getLogger("agent.agent")


__all__ = [
    "run_impl", "run_impl_reflective", "team_run_impl",
    "think", "think_stream",
    "execute_tool_calls_parallel", "execute_tool_calls_parallel_reflective",
]


async def team_run_impl(agent, task: str, session_id: str, user_id: str, user_name: str) -> AgentResult:
    """团队执行入口"""
    team_config = agent._team_config
    team_members = agent._team_members
    team_name = team_config.get("name", "未知团队")

    logger.info(f"[{agent.name}] 团队模式启动: {team_name}")
    wt_manager = None
    try:
        wt_manager = WorktreeManager(agent.workspace)
    except Exception:
        pass

    orchestrator = TeamOrchestrator(
        team_name=team_name,
        team_config=team_config,
        members=team_members,
        subagent_manager=getattr(agent, 'subagent_manager', None),
        llm_client=agent.client,
        memory_manager=getattr(agent, 'memory', None),
        pipeline_mode=team_config.get("pipeline_mode", "auto"),
        progress_callback=getattr(agent, '_progress_callback', None),
        parent_session_id=session_id or "",
        agent_pool=agent._agent_pool if hasattr(agent, '_agent_pool') else None,
        worktree_manager=wt_manager,
        max_parallel=agent._max_parallel,
        enable_parallel=agent._enable_parallel,
    )

    try:
        result = await orchestrator.run(task)
        status = "completed" if not result.startswith("ERROR:") else "failed"
        return AgentResult(agent_id=f"team:{team_name}", status=status, result=result)
    except Exception as e:
        logger.error(f"团队编排异常: {e}")
        return AgentResult(agent_id=f"team:{team_name}", status="failed", result=str(e))
