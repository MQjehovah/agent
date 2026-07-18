"""
Agent 工具执行模块 — execute_tool_safe / execute_tool / execute_subagent

提取自 agent.py，函数第一个参数为 agent 实例。
"""
import asyncio
import json
import logging
import os


# 延迟导入 current_run，避免与 agent.py 的循环导入问题
def _current_run():
    from agent.core import current_run
    return current_run()

# 工具输出最大字符数（与 agent.py 保持一致）
MAX_TOOL_OUTPUT_CHARS = int(os.environ.get("MAX_TOOL_OUTPUT_CHARS", 5000))

logger = logging.getLogger("agent.agent")


async def execute_tool_safe(agent, name: str, args: dict) -> str:
    """带权限检查、沙箱拦截、钩子、熔断器和错误恢复的工具执行"""
    cb = getattr(agent, '_circuit_breaker', None)
    if cb and name != "ask_user":
        if not cb.allow_request():
            logger.warning(f"熔断器开启，拒绝工具调用: {name}")
            return cb.get_fallback()

    perm_result = agent.permission.check(name, args)
    if not perm_result:
        logger.warning(f"工具调用被拦截: {name}, 原因: {perm_result.reason}")
        return json.dumps({"success": False, "error": perm_result.reason}, ensure_ascii=False)

    role = _current_run().session.role if _current_run().session else ""
    if agent.rbac and role and not agent.rbac.check_tool(role, name):
        logger.warning(f"RBAC: 角色 [{role}] 无权执行工具 [{name}]")
        return "抱歉，您当前没有使用该功能的权限，请联系管理员开通。"

    if perm_result.reason == "需要用户确认" and agent.on_confirm:
        try:
            confirmed = await agent.on_confirm(name, args)
            if not confirmed:
                return json.dumps({"success": False, "error": "用户拒绝执行此操作"}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"用户确认回调异常: {e}")
            return json.dumps({"success": False, "error": f"用户确认回调异常: {e}"}, ensure_ascii=False)

    sandbox_result = await _sandbox_intercept(agent, name, args)
    if sandbox_result is not None:
        return sandbox_result

    await agent.hooks.fire(agent._hook_event.PRE_TOOL_USE, tool_name=name, arguments=args)

    if agent.plugin_manager:
        for plugin in agent.plugin_manager.plugins.values():
            if plugin.enabled:
                try:
                    intercepted = await plugin.on_pre_tool_call(name, args)
                    if intercepted is not None:
                        logger.info(f"[插件拦截] {plugin.name} 拦截了工具调用: {name}")
                        return json.dumps(intercepted, ensure_ascii=False)
                except Exception as e:
                    logger.error(f"插件 [{plugin.name}] on_pre_tool_call 异常: {e}")

    agent.tracer.start_span(f"tool.{name}")

    args_preview = json.dumps(args, ensure_ascii=False)
    if len(args_preview) > 500:
        args_preview = args_preview[:500] + "..."
    logger.info(f"[工具调用] {name} | 输入: {args_preview}")

    await agent.hooks.fire(agent._hook_event.TOOL_START, tool_name=name, arguments=args)

    try:
        result = await execute_tool(agent, name, args)

        cb = getattr(agent, '_circuit_breaker', None)
        if cb and name != "ask_user":
            cb.on_success()

        if name == "file" and args.get("operation") == "read":
            path = args.get("path", "")
            if path and '"success": true' in result:
                try:
                    parsed = json.loads(result)
                    content = parsed.get("content", "")
                    if content:
                        agent.track_file_read(path, content)
                except (json.JSONDecodeError, ValueError):
                    pass

        result_preview = result
        if len(result_preview) > 500:
            result_preview = result_preview[:500] + "..."
        logger.info(f"[工具返回] {name} | 输出: {result_preview}")

        await agent.hooks.fire(agent._hook_event.TOOL_RESULT, tool_name=name, result=result_preview)

        from tool_result_compressor import compress_tool_result
        if len(result) > MAX_TOOL_OUTPUT_CHARS:
            original_len = len(result)
            result = compress_tool_result(name, result, MAX_TOOL_OUTPUT_CHARS)
            logger.debug(f"[工具压缩] {name}: {original_len} -> {len(result)} chars")

        return result
    except Exception as e:
        logger.error(f"工具 {name} 执行失败: {e}")
        cb = getattr(agent, '_circuit_breaker', None)
        if cb and name != "ask_user":
            cb.on_failure()
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


async def execute_tool(agent, name: str, args: dict) -> str:
    """执行工具（根据名称分发到对应的工具实现）"""
    rc = _current_run()
    current_uid = ""
    if rc.session and rc.session.user_id:
        current_uid = rc.session.user_id
    elif rc.user_id:
        current_uid = rc.user_id

    try:
        if name == "subagent" and agent.subagent_manager:
            return await execute_subagent(agent, args)

        if agent.tool_registry and agent.tool_registry.has_tool(name):
            if name == "memory":
                args["_local_user_id"] = current_uid
            return await agent.tool_registry.execute(name, args)

        if agent.skill_manager and name in ("skill", "execute_skill"):
            return await agent.skill_manager.execute_tool(name, args)

        if agent.mcp and agent.mcp.has_tool(name):
            return await agent.mcp.call_tool(name, args)

        if agent.plugin_manager:
            for plugin in agent.plugin_manager.plugins.values():
                logger.debug(f"检查插件 {plugin.name}, enabled={plugin.enabled}")
                if plugin.enabled:
                    tool_defs = plugin.get_tool_defs()
                    logger.debug(f"插件 {plugin.name} 工具定义: {[t.get('function', {}).get('name') for t in tool_defs]}")
                    if any(t.get("function", {}).get("name") == name for t in tool_defs):
                        logger.info(f"执行插件工具: {plugin.name}.{name}")
                        if current_uid:
                            args["_local_user_id"] = current_uid
                        return await plugin.execute_tool(name, args)

        return f"工具 {name} 不存在"
    except Exception as e:
        return f"工具执行错误: {e}"


async def execute_subagent(agent, args: dict) -> str:
    """创建并执行子代理"""
    agent_name = args.get("name", "")
    template_name = args.get("template", "")
    task = args.get("task", "")
    display_name = agent_name or template_name or "?"

    if not task:
        return json.dumps({"success": False, "error": "缺少 task 参数"}, ensure_ascii=False)

    role = _current_run().session.role if _current_run().session else ""
    if agent.rbac and role and agent_name and not agent.rbac.check_agent(role, agent_name):
        logger.warning(f"RBAC: 角色 [{role}] 无权访问子代理 [{agent_name}]")
        return "抱歉，您当前没有使用该功能的权限，请联系管理员开通。"

    await agent.hooks.fire(agent._hook_event.SUBAGENT_START, metadata={"name": display_name, "task": task})

    try:
        if agent.subagent_manager and agent.subagent_manager.is_team(template_name):
            def _team_progress(stage, status, info, extra=None):
                asyncio.ensure_future(agent.hooks.fire(
                    agent._hook_event.SUBAGENT_PROGRESS,
                    metadata={"stage": stage, "status": status, "info": info, "extra": extra, "team": display_name},
                ))

            team_result = await agent.subagent_manager._run_team_orchestrator(
                task, template_name,
                client=agent.client,
                progress_callback=_team_progress,
                parent_session_id=args.get("session_id", ""))
            # AgentResult dataclass → 字符串
            result = team_result.result if hasattr(team_result, 'result') else str(team_result)
        else:
            instance, _ = await agent.subagent_manager.get_or_create_subagent(
                template=args.get("template", ""),
                name=args.get("name", ""),
                session_id=args.get("session_id", ""),
                system_prompt=args.get("system_prompt", ""),
                tools=args.get("tools"),
                mcp_servers=args.get("mcp_servers"),
                client=agent.client,
                parent_agent=agent,
            )
            sub_agent = instance.agent
            sub_sid = instance.session_id

            user_id = _current_run().user_id or "cli:admin"
            user_name = _current_run().user_name or "管理员"
            r = await sub_agent.run(task, session_id=sub_sid, user_id=user_id, user_name=user_name)
            text = r.result if hasattr(r, 'result') else str(r)

            if args.get("keep_alive", True):
                await agent.subagent_manager.cleanup_subagent(instance.session_id)

            result = text

        await agent.hooks.fire(agent._hook_event.SUBAGENT_RESULT, metadata={
            "name": display_name, "status": "completed" if result else "failed", "result": result[:3000] if result else "",
        })
        return json.dumps({
            "success": True,
            "agent_id": f"team:{display_name}" if agent.subagent_manager and agent.subagent_manager.is_team(template_name) else display_name,
            "status": "completed",
            "result": result,
        }, ensure_ascii=False)

    except Exception as e:
        logger.error(f"Subagent execution error: {e}")
        await agent.hooks.fire(agent._hook_event.SUBAGENT_RESULT, metadata={"name": display_name, "error": str(e)})
        return json.dumps({"success": False, "error": f"子代理执行错误: {e}"}, ensure_ascii=False)


async def _sandbox_intercept(agent, name: str, args: dict) -> str | None:
    """沙箱中间层拦截"""
    if not agent.sandbox or not agent.sandbox.should_intercept(name, args):
        return None
    if name == "shell":
        result = await agent.sandbox.execute_shell(args)
        if result is not None:
            logger.info(f"[沙箱拦截] shell → {result.get('sandbox', '?')}")
            return json.dumps(result, ensure_ascii=False)
    if name in ("file", "edit"):
        path = args.get("path", "")
        if path:
            valid, reason = agent.sandbox.validate_path(path)
            if not valid:
                return json.dumps({"success": False, "error": reason}, ensure_ascii=False)
    return None


async def run_reflection(agent, learner, task: str, messages: list, user_id: str = ""):
    """后台执行任务反思"""
    try:
        logger.info(f"[自学习] 开始任务反思, 消息数: {len(messages)}")
        saved = await learner.reflect_on_task(task, messages, user_id)
        if saved > 0:
            logger.info(f"[自学习] 任务反思完成，保存了 {saved} 条经验")
        else:
            logger.info("[自学习] 任务反思完成，无新经验保存")
    except Exception as e:
        logger.warning(f"[自学习] 任务反思失败: {e}", exc_info=True)


def has_token_subscribers(agent) -> bool:
    """检查是否有流式 token 订阅者"""
    return bool(agent.hooks._hooks.get(agent._hook_event.CHAT_EVENT))


def parse_user_id() -> tuple[str, str]:
    """解析用户 ID 为 platform 和 uid"""
    uid = _current_run().user_id
    if ":" in uid:
        platform_, uid = uid.split(":", 1)
        return platform_, uid
    return "dingtalk", uid


def register_subagent_hooks(agent, sub_agent, agent_name: str):
    """在子代理上注册事件转发钩子"""
    mapping = {
        agent._hook_event.TOOL_START: agent._hook_event.SUBAGENT_TOOL_START,
        agent._hook_event.TOOL_RESULT: agent._hook_event.SUBAGENT_TOOL_RESULT,
        agent._hook_event.ROUND_START: agent._hook_event.SUBAGENT_ROUND_START,
        agent._hook_event.CHAT_EVENT: agent._hook_event.SUBAGENT_CHAT_EVENT,
        agent._hook_event.LLM_RESPONSE: agent._hook_event.SUBAGENT_LLM_RESPONSE,
    }
    unregisters = []
    for src_evt, dst_evt in mapping.items():
        async def _forward(ctx, _dst=dst_evt, _name=agent_name):
            await agent.hooks.fire(_dst, metadata={
                "name": _name, **ctx.metadata, "content": getattr(ctx, "content", ""),
                "reasoning": getattr(ctx, "reasoning", ""),
                "tool_name": getattr(ctx, "tool_name", ""),
                "arguments": getattr(ctx, "arguments", {}),
                "result": getattr(ctx, "result", ""),
            })
        sub_agent.hooks.register(src_evt, _forward)
        unregisters.append((sub_agent, src_evt, _forward))

    # 转发 SUBAGENT_PROGRESS
    async def _forward_progress(ctx):
        await agent.hooks.fire(agent._hook_event.SUBAGENT_PROGRESS, metadata={
            "name": agent_name, **(ctx.metadata or {}),
        })
    sub_agent.hooks.register(agent._hook_event.SUBAGENT_PROGRESS, _forward_progress)
    unregisters.append((sub_agent, agent._hook_event.SUBAGENT_PROGRESS, _forward_progress))

    return unregisters


def unregister_subagent_hooks(agent, unregisters: list):
    """注销子代理的事件转发钩子"""
    if not unregisters:
        return
    for sub_agent, event, callback in unregisters:
        try:
            sub_agent.hooks.unregister(event, callback)
        except Exception:
            pass
