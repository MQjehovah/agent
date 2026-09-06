"""运行分发器（RunDispatcher）—— Wave B Step3。

把 `Agent.run()` 中“按形态选执行实现”的分支收敛到单一入口：

- 团队(TEAM)      → team_run_impl(流水线编排)
- reflective 循环 → run_impl_reflective(计划→执行→评估)
- 默认 ReAct       → run_impl

目的：
1. core.run 不再堆积形态 if/else，新增形态只需在此登记；
2. 后续可在此统一做运行前/后钩子(用量、审计、队列)而不侵入各 impl。
"""

import logging

logger = logging.getLogger("agent.runner")


async def dispatch(agent, task: str, session_id: str, user_id: str, user_name: str, inherited):
    """根据 agent 形态分发到对应 run 实现，返回 AgentResult。"""
    if getattr(agent, "_is_team", False) and getattr(agent, "_team_config", None) \
            and getattr(agent, "_team_members", None):
        from agent.loop import team_run_impl
        return await team_run_impl(agent, task, session_id, user_id, user_name)

    if getattr(agent, "loop_mode", "react") == "reflective":
        from agent.loop import run_impl_reflective
        return await run_impl_reflective(agent, task, session_id, user_id, user_name, inherited)

    from agent.loop import run_impl
    return await run_impl(agent, task, session_id, user_id, user_name, inherited)
