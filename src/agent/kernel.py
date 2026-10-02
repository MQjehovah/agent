"""Agent 内核边界 —— 运行入口与形态分发。

内核只负责"把一次 run 分发到对应循环实现"，并做运行前的轻量预处理（Plan Mode）：

- 团队(TEAM)      → `team_run_impl`（流水线编排）
- reflective 循环 → `run_impl_reflective`（计划→执行→评估）
- 默认 ReAct       → `run_impl`

边界约定（对齐 Pi 的 harness/framework 分层）：
- 内核**不导入任何渠道**（web/钉钉/飞书/桌面）与**具体能力来源**（MCP/插件/市场）；
  这些由循环实现或 `CapabilityProvider` 承接，内核只面对 `Agent` 抽象。
- 内核产生结构化事件经 `agent.events`（`AgentEvent`），消费者自行订阅。
- 新增运行形态只需在此登记，`core.run` 不再堆积形态分支。
"""
import logging

logger = logging.getLogger("agent.kernel")


def _make_plan_confirmer(agent):
    """把 agent.on_confirm 适配为 PlanMode 期望的 (plan)->bool 回调。"""
    async def _confirm(plan) -> bool:
        cb = getattr(agent, "on_confirm", None)
        if cb is None:
            return True
        try:
            return bool(await cb("__plan__", {"title": plan.title, "plan": plan.to_markdown()}))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"计划审批回调异常，默认放行: {e}")
            return True
    return _confirm


async def _maybe_apply_plan(agent, task: str) -> str:
    """启用 Plan Mode 时：生成计划→审批→把已批准计划附到任务前。

    未启用 / 不需要规划 / 生成失败 / 审批拒绝时均原样返回 task，绝不阻断执行。
    """
    if not getattr(agent, "_enable_plan_mode", False):
        return task
    try:
        pm = getattr(agent, "_plan_mode", None)
        cfg = getattr(agent, "_plan_mode_config", {}) or {}
        if pm is None:
            from plan_mode import PlanMode
            pm = PlanMode(
                client=getattr(agent, "client", None),
                workspace=getattr(agent, "workspace", "") or "",
                auto_plan=cfg.get("auto_plan", True),
                require_approval=cfg.get("require_approval", True),
            )
            pm.on_confirm = _make_plan_confirmer(agent)
            agent._plan_mode = pm
        if not pm.should_plan(task):
            return task
        plan = await pm.generate_plan(task)
        if not await pm.present_plan(plan):
            logger.info("[plan] 用户拒绝计划，按原任务继续")
            return task
        logger.info(f"[plan] 已批准计划: {plan.title}（{len(plan.steps)} 步）")
        return f"{task}\n\n[已批准的执行计划，请严格遵循]\n{plan.to_markdown()}"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[plan] 计划生成/审批跳过: {e}")
        return task


async def dispatch(agent, task: str, session_id: str, user_id: str, user_name: str, inherited):
    """根据 agent 形态分发到对应 run 实现，返回 AgentResult。"""
    task = await _maybe_apply_plan(agent, task)
    if getattr(agent, "_is_team", False) and getattr(agent, "_team_config", None) \
            and getattr(agent, "_team_members", None):
        from agent.loop import team_run_impl
        return await team_run_impl(agent, task, session_id, user_id, user_name)

    if getattr(agent, "loop_mode", "react") == "reflective":
        from agent.loop import run_impl_reflective
        return await run_impl_reflective(agent, task, session_id, user_id, user_name, inherited)

    from agent.loop import run_impl
    return await run_impl(agent, task, session_id, user_id, user_name, inherited)


# 内核统一运行入口别名（新代码建议用 run_kernel；dispatch 保留兼容）
run_kernel = dispatch
