"""
Agent ReAct 循环 — think → execute → repeat

从 agent/loop.py 提取，函数第一个参数为 agent 实例。
"""
import asyncio
import contextlib
import json
import logging

from agent.context import AgentResult
from agent.executor import execute_tool_safe

logger = logging.getLogger("agent.agent")


async def think(agent, messages) -> dict:
    """调用 LLM 思考（非流式）"""
    try:
        response = await agent.client.chat(
            messages,
            agent.tool_defs,
        )
        msg = response.choices[0].message

        content_preview = (msg.content or "")[:200]
        logger.info(
            f"[LLM响应] model: {response.model} | content: {content_preview or '(空)'} "
            f"| tool_calls: {len(msg.tool_calls) if msg.tool_calls else 0}")

        tool_calls = None
        if msg.tool_calls:
            tool_calls = []
            for tc in msg.tool_calls:
                func_args = tc.function.arguments
                if isinstance(func_args, str):
                    try:
                        json.loads(func_args)
                    except (json.JSONDecodeError, ValueError):
                        try:
                            func_args = json.dumps(func_args, ensure_ascii=False)
                        except (TypeError, ValueError):
                            func_args = "{}"
                elif isinstance(func_args, dict):
                    logger.warning(f"Agent [{agent.name}] function.arguments is dict, converting to JSON string")
                    func_args = json.dumps(func_args, ensure_ascii=False)
                else:
                    logger.warning(f"Agent [{agent.name}] function.arguments is {type(func_args)}, set to empty object")
                    func_args = "{}"
                tool_calls.append({
                    "id": tc.id, "type": tc.type,
                    "function": {"name": tc.function.name, "arguments": func_args},
                })

        return {
            "message": {
                "content": msg.content,
                "tool_calls": tool_calls,
                "reasoning_content": getattr(msg, "reasoning_content", None),
            }
        }
    except Exception as e:
        logger.error(f"Agent [{agent.name}] think error: {e}")
        return {"message": {"content": f"思考出错: {e}"}}


async def think_stream(agent, messages) -> dict:
    """流式思考模式"""
    content = ""
    reasoning_content = ""
    tool_calls_accumulator = {}

    async for chunk in agent.client.stream_chat(messages, agent.tool_defs):
        delta = chunk.choices[0].delta if chunk.choices else None
        if not delta:
            continue
        if delta and getattr(delta, "reasoning_content", None):
            reasoning_content += delta.reasoning_content
        if delta and delta.content:
            content += delta.content
            await agent.hooks.fire(agent._hook_event.CHAT_EVENT, token=delta.content)
        if delta and delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index
                if idx not in tool_calls_accumulator:
                    tool_calls_accumulator[idx] = {"id": tc.id, "type": tc.type, "function": {"name": "", "arguments": ""}}
                if tc.id:
                    tool_calls_accumulator[idx]["id"] = tc.id
                if tc.type:
                    tool_calls_accumulator[idx]["type"] = tc.type
                if tc.function:
                    if tc.function.name:
                        tool_calls_accumulator[idx]["function"]["name"] += tc.function.name
                    if tc.function.arguments:
                        tool_calls_accumulator[idx]["function"]["arguments"] += tc.function.arguments

    tool_calls = list(tool_calls_accumulator.values()) if tool_calls_accumulator else None
    return {
        "message": {
            "content": content,
            "tool_calls": tool_calls,
            "reasoning_content": reasoning_content,
        }
    }


async def execute_tool_calls_parallel(agent, tool_calls: list, session):
    """并行执行工具调用（react 模式）"""
    if len(tool_calls) <= 1:
        for tc in tool_calls:
            func_name = tc.get("function", {}).get("name", "")
            func_args = tc.get("function", {}).get("arguments", {})
            if isinstance(func_args, str):
                try:
                    func_args = json.loads(func_args)
                except (json.JSONDecodeError, ValueError):
                    func_args = {}
            try:
                result = await execute_tool_safe(agent, func_name, func_args)
                session.add_message("tool", str(result), name=func_name, tool_call_id=tc.get("id", ""))
            except Exception as e:
                logger.error(f"工具执行异常: {e}")
                session.add_message("tool", f"工具执行异常: {e}", name=func_name, tool_call_id=tc.get("id", ""))
        return

    async def _run_one(tc):
        func_name = tc.get("function", {}).get("name", "")
        func_args = tc.get("function", {}).get("arguments", {})
        if isinstance(func_args, str):
            try:
                func_args = json.loads(func_args)
            except (json.JSONDecodeError, ValueError):
                func_args = {}
        return tc, await execute_tool_safe(agent, func_name, func_args)

    tasks = [asyncio.create_task(_run_one(tc)) for tc in tool_calls]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, item in enumerate(results):
            tc = tool_calls[i]
            func_name = tc.get("function", {}).get("name", "")
            tc_id = tc.get("id", "")
            if isinstance(item, asyncio.CancelledError):
                logger.warning("工具执行被取消")
                session.add_message("tool", "工具执行被取消", name=func_name, tool_call_id=tc_id)
            elif isinstance(item, Exception):
                logger.error(f"工具执行异常: {item}")
                session.add_message("tool", f"工具执行异常: {item}", name=func_name, tool_call_id=tc_id)
            else:
                _, result = item
                session.add_message("tool", str(result), name=func_name, tool_call_id=tc_id)
    except asyncio.CancelledError:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def execute_tool_calls_parallel_reflective(agent, tool_calls: list, session) -> bool:
    """v2: 并行执行工具（返回是否有错误）"""
    had_errors = False
    if len(tool_calls) <= 1:
        for tc in tool_calls:
            func_name = tc.get("function", {}).get("name", "")
            func_args = tc.get("function", {}).get("arguments", {})
            if isinstance(func_args, str):
                try:
                    func_args = json.loads(func_args)
                except (json.JSONDecodeError, ValueError):
                    func_args = {}
            try:
                result = await execute_tool_safe(agent, func_name, func_args)
                session.add_message("tool", str(result), name=func_name, tool_call_id=tc.get("id", ""))
                had_errors = had_errors or ("工具执行异常" in str(result) or "ERROR" in str(result)[:10])
            except Exception as e:
                logger.error(f"工具执行异常: {e}")
                session.add_message("tool", f"工具执行异常: {e}", name=func_name, tool_call_id=tc.get("id", ""))
                had_errors = True
        return had_errors

    async def _run_one(tc):
        func_name = tc.get("function", {}).get("name", "")
        func_args = tc.get("function", {}).get("arguments", {})
        if isinstance(func_args, str):
            try:
                func_args = json.loads(func_args)
            except (json.JSONDecodeError, ValueError):
                func_args = {}
        return tc, await execute_tool_safe(agent, func_name, func_args)

    tasks = [asyncio.create_task(_run_one(tc)) for tc in tool_calls]
    had_errors = False
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, item in enumerate(results):
            tc = tool_calls[i]
            func_name = tc.get("function", {}).get("name", "")
            tc_id = tc.get("id", "")
            if isinstance(item, asyncio.CancelledError):
                logger.warning("工具执行被取消")
                session.add_message("tool", "工具执行被取消", name=func_name, tool_call_id=tc_id)
                had_errors = True
            elif isinstance(item, Exception):
                logger.error(f"工具执行异常: {item}")
                session.add_message("tool", f"工具执行异常: {item}", name=func_name, tool_call_id=tc_id)
                had_errors = True
            else:
                _, result = item
                session.add_message("tool", str(result), name=func_name, tool_call_id=tc_id)
                had_errors = had_errors or ("工具执行异常" in str(result) or "ERROR" in str(result)[:10])
    except asyncio.CancelledError:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return had_errors


# ── Shared helpers ──────────────────────────────────


def _resolve_run_context(agent, inherited, session_id):
    """设置 RunContext 和 session（run_impl / run_impl_reflective 共享）"""
    from agent.context import current_run
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

    if session and session.messages:
        from agent.core import Agent
        session.messages = Agent._apply_system_messages(
            session.messages, agent.system_static, agent.system_dynamic)

    return session, rc


async def _compress_and_trace(messages, agent, session_id, writeback=None):
    """上下文压缩 + token 追踪（run_impl / run_impl_reflective 共享）"""
    from conversation.session import AgentSessionManager
    try:
        compressed = await AgentSessionManager.compress_if_needed(
            messages, agent.client, tool_defs=agent.tool_defs, session_id=session_id,
        )
        if compressed is not messages:
            messages = compressed
            if writeback is not None:
                writeback.messages = compressed
    except Exception as e:
        logger.warning(f"上下文压缩失败(跳过): {e}")
    try:
        ctx_est = AgentSessionManager.estimate_tokens(messages, agent.tool_defs)
        agent.tracer.record_context_size(ctx_est)
    except Exception:
        pass
    return messages


def _rollback_tool_messages(session):
    """工具执行异常时回滚消息"""
    while session.messages and session.messages[-1].get("role") == "tool":
        session.messages.pop()
    if session.messages and session.messages[-1].get("role") == "assistant":
        session.messages.pop()


async def _finalize_run(agent, ctx, task, session, user_id):
    """后台反思 + AGENT_STOP hook（run_impl / run_impl_reflective 共享）"""
    if hasattr(agent, 'learner') and agent.learner and agent._learning_per_round and session and len(session.messages) > 1:
        from agent.executor import run_reflection
        bg_task = asyncio.create_task(run_reflection(agent, agent.learner, task, list(session.messages), user_id))
        agent._background_tasks.add(bg_task)
        bg_task.add_done_callback(agent._background_tasks.discard)
    await agent.hooks.fire(agent._hook_event.AGENT_STOP, metadata={
        "status": ctx.status, "result_length": len(ctx.result) if ctx.result else 0,
    })


# ── React 循环 ─────────────────────────────────────

async def run_impl(agent, task: str, session_id: str, user_id: str, user_name: str, inherited) -> AgentResult:
    """react 循环：思考 → 执行 → 重复"""
    session, rc = _resolve_run_context(agent, inherited, session_id)
    ctx = rc
    if user_id:
        ctx.user_id = user_id
    if user_name:
        ctx.user_name = user_name

    try:
        i = 0
        while i < agent.max_iterations:
            if agent._shutdown_event and agent._shutdown_event.is_set():
                ctx.status = "cancelled"
                break
            if agent._cancel_flag and agent._cancel_flag.is_set():
                ctx.status = "cancelled"
                break

            try:
                ctx_tokens = agent.tracer.get_context_stats().get("final", 0)
                agent.tracer.start_span("agent.think")
                usage_summary = agent.client.usage_tracker.get_summary()
                logger.info(
                    f"[{agent.name}] [{session.session_id if session else ''}] 开始思考 | "
                    f"轮次 {i + 1}/{agent.max_iterations} | "
                    f"上下文 {ctx_tokens:,}token | "
                    f"累计 {usage_summary['total_calls']}次 "
                    f"{usage_summary['total_prompt_tokens']:,}+{usage_summary['total_completion_tokens']:,}token "
                    f"¥{usage_summary['total_cost_cny']}"
                )
                if i > 0:
                    await agent.hooks.fire(agent._hook_event.ROUND_START, metadata={"iteration": i + 1})

                think_messages = session.messages
                _is_retry = bool(ctx.retry_context)
                if ctx.retry_context:
                    think_messages = list(session.messages)
                    think_messages.append({"role": "user", "content": ctx.retry_context})
                    ctx.retry_context = ""

                think_messages = await _compress_and_trace(
                    think_messages, agent, session.session_id if session else '',
                    writeback=None if _is_retry else session)

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
                        await execute_tool_calls_parallel(agent, msg["tool_calls"], session)
                    except BaseException:
                        _rollback_tool_messages(session)
                        raise
                    ctx.consecutive_errors = 0
                    i += 1
                    continue

                if msg.get("content"):
                    ctx.status = "completed"
                    ctx.result = msg.get("content")
                    ctx.retry_context = ""
                    break

                i += 1

            except Exception as e:
                ctx.consecutive_errors += 1
                logger.error(f"Agent [{agent.name}] 第 {i+1} 轮出错: {e}")
                agent.tracer.end_span(status="error")

                from quality.error_classifier import ErrorClassifier
                recovery_hint = ""
                try:
                    err_type = ErrorClassifier.classify(e, {"func_name": "agent_think", "error_count": ctx.consecutive_errors})
                    rec = ErrorClassifier.get_recovery(err_type)
                    recovery_hint = f"\n错误类型: {err_type.value if hasattr(err_type, 'value') else err_type}\n建议: {rec}\n"
                except Exception:
                    pass

                if ctx.consecutive_errors >= 3:
                    ctx.status = "failed"
                    ctx.result = f"连续 {ctx.consecutive_errors} 次思考出错"
                    break

                ctx.retry_context = f"上一轮思考出错: {e}。{recovery_hint}请分析错误原因，尝试用其他方式继续完成任务。"
                i += 1
                continue
        else:
            if ctx.status == "pending":
                ctx.status = "max_iterations"
                ctx.result = "达到最大迭代次数"
                logger.warning(f"Agent [{agent.name}] max iterations reached")

    except asyncio.CancelledError:
        logger.warning(f"Agent [{agent.name}] 任务被取消")
        ctx.status = "cancelled"
    except Exception as e:
        ctx.status = "failed"
        logger.error(f"Agent [{agent.name}] [{session.session_id if session else ''}] failed: {e}")

    await _finalize_run(agent, ctx, task, session, user_id)


async def run_impl_reflective(agent, task: str, session_id: str, user_id: str, user_name: str, inherited) -> AgentResult:
    """reflective 循环：计划 → 执行 → 观察 → 评估 → 调整 → 重复"""
    session, rc = _resolve_run_context(agent, inherited, session_id)
    ctx = rc
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
                ctx.status = "cancelled"
                break
            if agent._cancel_flag and agent._cancel_flag.is_set():
                ctx.status = "cancelled"
                break

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

                think_messages = await _compress_and_trace(
                    think_messages, agent, session.session_id if session else '',
                    writeback=session)

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
                        await execute_tool_calls_parallel_reflective(agent, msg["tool_calls"], session)
                    except BaseException:
                        _rollback_tool_messages(session)
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


# ── Team execution ────────────────────────────────────


async def team_run_impl(agent, task: str, session_id: str, user_id: str, user_name: str) -> AgentResult:
    """团队执行入口"""
    from team.orchestrator import TeamOrchestrator
    from team.worktree import WorktreeManager

    team_config = agent._team_config
    team_members = agent._team_members
    team_name = team_config.get("name", "未知团队")

    logger.info(f"[{agent.name}] 团队模式启动: {team_name}")
    wt_manager = None
    with contextlib.suppress(Exception):
        wt_manager = WorktreeManager(agent.workspace)

    orchestrator = TeamOrchestrator(
        team_name=team_name,
        team_config=team_config,
        members=team_members,
        agent=agent,
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

