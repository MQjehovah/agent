async def run_impl_reflective(agent, task: str, session_id: str, user_id: str, user_name: str, inherited) -> AgentResult:
    """reflective 循环：计划 → 执行 → 观察 → 评估 → 调整 → 重复"""
    from agent.core import current_run
    rc = current_run()
    session = rc.session
    if session is None and not agent.parent_agent:
        session = inherited.session if hasattr(inherited, 'session') and inherited.session else None
    if session is None:
        from dataclasses import dataclass, field
        @dataclass
        class _MinSession:
            messages: list = field(default_factory=list)
            session_id: str = ""
            def add_message(self, role, content, **kwargs):
                msg = {"role": role, "content": content}
                for key in ("tool_call_id", "name", "tool_calls"):
                    if key in kwargs and kwargs[key]:
                        msg[key] = kwargs[key]
                self.messages.append(msg)
        session = _MinSession()
    if session and session_id:
        session.session_id = session_id

    rc.agent_id = agent.agent_id
    ctx = rc

    if not rc.system_prompt:
        rc.system_prompt = agent.system_prompt
        rc.system_static = agent.system_static
        rc.system_dynamic = agent.system_dynamic

    if session and session.messages:
        from agent.core import Agent
        session.messages = Agent._apply_system_messages(
            session.messages, rc.system_static, rc.system_dynamic)

    if user_id:
        ctx.user_id = user_id
    if user_name:
        ctx.user_name = user_name

    phase = "plan"
    plan_rounds = 0
    execute_rounds = 0
    max_execute_rounds_before_eval = 3
    plan_prompt_injected = False

    try:
        i = 0
        while i < agent.max_iterations:
            if agent._shutdown_event and agent._shutdown_event.is_set():
                ctx.status = "cancelled"; break
            if agent._cancel_flag and agent._cancel_flag.is_set():
                ctx.status = "cancelled"; break

            try:
                ctx_tokens = agent.tracer.get_context_stats().get("final", 0)
                agent.tracer.start_span("agent.think")
                logger.info(f"[{agent.name}] reflective phase={phase} 第 {i+1}/{agent.max_iterations} 轮 | ctx {ctx_tokens:,}t")

                if i > 0:
                    await agent.hooks.fire(agent._hook_event.ROUND_START, metadata={"iteration": i + 1})

                think_messages = list(session.messages)

                if phase == "plan" and not plan_prompt_injected:
                    plan_prompt = (
                        "【制定计划】请先分析任务，制定一个清晰的执行计划。\n\n"
                        "计划应包含：\n"
                        "1. 目标：明确本次任务要达成的目标\n"
                        "2. 步骤：列出具体执行步骤，每个步骤用什么工具\n"
                        "3. 预期产出：每个步骤的预期结果\n\n"
                        "请先输出计划，计划确认后开始执行。"
                    )
                    think_messages.append({"role": "user", "content": plan_prompt})
                    plan_prompt_injected = True
                    plan_rounds += 1
                elif phase == "evaluate":
                    evaluate_prompt = (
                        "【评估进展】请检查当前执行结果：\n\n"
                        "1. 目标达成度：已完成了多少？还差什么？\n"
                        "2. 是否遇到障碍：有什么问题需要解决？\n"
                        "3. 下一步：\n"
                        "   - 如果目标已基本达成 → 直接输出最终结果，不要调工具\n"
                        "   - 如果还有工作要做 → 调整计划后继续执行\n"
                        "   - 如果方案行不通 → 换一种方案重新来"
                    )
                    think_messages.append({"role": "user", "content": evaluate_prompt})
                    phase = "execute"
                    execute_rounds = 0
                elif ctx.retry_context:
                    think_messages.append({"role": "user", "content": ctx.retry_context})
                    ctx.retry_context = ""

                # 上下文压缩
                from agent.session import AgentSessionManager
                try:
                    compressed = await AgentSessionManager.compress_if_needed(
                        think_messages, agent.client,
                        tool_defs=agent.tool_defs,
                        session_id=session.session_id if session else "",
                    )
                    if compressed is not think_messages:
                        think_messages = compressed
                        session.messages = compressed
                except Exception as e:
                    logger.warning(f"上下文压缩失败(跳过): {e}")

                # 记录上下文 token 数
                try:
                    ctx_est = AgentSessionManager.estimate_tokens(
                        think_messages, agent.tool_defs)
                    agent.tracer.record_context_size(ctx_est)
                except Exception:
                    pass

                response = await think(agent, think_messages)
                agent.tracer.end_span()

                msg = response.get("message", {})
                content = msg.get("content") or ""
                if content:
                    await agent.hooks.fire(agent._hook_event.LLM_RESPONSE,
                                           content=content,
                                           reasoning=getattr(msg, "reasoning_content", None) or "")

                session.add_message("assistant", msg.get("content") or "",
                                    tool_calls=msg.get("tool_calls"),
                                    reasoning_content=msg.get("reasoning_content"))

                if msg.get("tool_calls"):
                    try:
                        had_errors = await execute_tool_calls_parallel_reflective(agent, msg["tool_calls"], session)
                    except BaseException:
                        while session.messages and session.messages[-1].get("role") == "tool":
                            session.messages.pop()
                        if session.messages and session.messages[-1].get("role") == "assistant":
                            session.messages.pop()
                        raise
                    ctx.consecutive_errors = 0
                    execute_rounds += 1
                    if phase == "plan":
                        phase = "execute"
                    if execute_rounds >= max_execute_rounds_before_eval:
                        phase = "evaluate"
                    i += 1
                    continue

                if msg.get("content"):
                    if phase == "plan" and plan_rounds <= 2:
                        phase = "execute"
                        i += 1
                        continue
                    ctx.status = "completed"
                    ctx.result = msg.get("content")
                    ctx.retry_context = ""
                    break

                i += 1

            except Exception as e:
                ctx.consecutive_errors += 1
                logger.error(f"Agent [{agent.name}] reflective 第 {i+1} 轮出错: {e}")
                agent.tracer.end_span(status="error")
                if ctx.consecutive_errors >= 3:
                    ctx.status = "failed"
                    ctx.result = f"连续 {ctx.consecutive_errors} 次出错"
                    break
                ctx.retry_context = f"上一轮出错: {e}，请用其他方式继续。"
                i += 1
                continue
        else:
            if ctx.status == "pending":
                ctx.status = "max_iterations"
                ctx.result = "达到最大迭代次数"
                logger.warning(f"Agent [{agent.name}] reflective max iterations")

    except asyncio.CancelledError:
        logger.warning(f"Agent [{agent.name}] reflective 被取消")
        ctx.status = "cancelled"
    except Exception as e:
        ctx.status = "failed"
        logger.error(f"Agent [{agent.name}] reflective 失败: {e}")

    return AgentResult(agent_id=agent.agent_id, status=ctx.status, result=ctx.result or "")


# ── 团队循环 ──────────────────────────────────────
