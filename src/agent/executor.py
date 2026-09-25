"""
Agent 工具执行模块 — execute_tool_safe / execute_tool / execute_subagent

提取自 agent.py，函数第一个参数为 agent 实例。
"""
import asyncio
import contextlib
import json
import logging
import os
import time


# 延迟导入 current_run，避免与 agent.py 的循环导入问题
def _current_run():
    from agent.core import current_run
    return current_run()

# 工具输出最大字符数（与 agent.py 保持一致）
MAX_TOOL_OUTPUT_CHARS = int(os.environ.get("MAX_TOOL_OUTPUT_CHARS", 5000))

# MCP 调用审计: 失败结果文案前缀(manager.call_tool 的已知失败出口)
_MCP_FAILURE_PREFIXES = ("执行失败", "MCP未连接", "MCP服务", "MCP [", "工具执行错误")

logger = logging.getLogger("agent.agent")


def _mark_run_sensitive_if_hit(agent, name: str) -> None:
    """命中敏感清单或 destructive MCP 工具 → 给当前 run 打「含敏感产出」标记(保守: 调过即敏感)。

    敏感清单见 agent.sensitive(可配置); MCP 工具按风险注解判定: destructive 注解的
    工具(如设备控制/终端命令)结果同样按敏感处理, 触发渠道层「钉钉群不落群、私聊送达」。
    在工具真正执行前置位(已过权限/RBAC/确认/Sandbox 拦截)，保证被允许执行的
    敏感工具一旦运行即为敏感；写入当前 run 的 RunContext.sensitive_hit，
    嵌套 run(子代理调用链)由 agent.core.run 结束时会聚到父级上下文。
    仅活跃 run(带 run_id)才打标，避免误改 run 之外的共享空上下文。
    """
    from agent.sensitive import is_sensitive_tool
    if not is_sensitive_tool(name):
        mcp = getattr(agent, "mcp", None)
        try:
            mcp_risk = mcp.tool_risk(name) if mcp else None
        except Exception:
            mcp_risk = None
        if mcp_risk != "destructive":
            return
    rc = _current_run()
    if rc is None or not getattr(rc, "run_id", ""):
        return
    rc.sensitive_hit = True


def _get_user_circuit_breaker(agent):
    """获取当前用户的独立熔断器。

    100+ 并发用户场景：熔断器按 user_id 隔离——单个用户连续工具失败
    只熔断该用户，不影响其他用户（避免"单用户拖垮全系统"）。
    无 user_id（如系统内部任务）时回退到 agent 级全局熔断器。
    """
    try:
        uid = _current_run().user_id
    except Exception:
        uid = ""
    if uid:
        from quality.circuit_breaker import get_circuit_breaker_for
        return get_circuit_breaker_for(uid)
    return getattr(agent, '_circuit_breaker', None)


def _mcp_call_failed(result: str) -> bool:
    """manager.call_tool 返回文本是否代表失败(前缀启发式, 与 manager 失败出口对齐)。"""
    return str(result or "").startswith(_MCP_FAILURE_PREFIXES)


def _record_mcp_call(agent, exposed_name: str, duration_ms: int,
                     result: str = "", error: str = "") -> None:
    """MCP 调用审计落库(成功/失败都记; 审计异常只告警, 不影响工具执行)。

    conversation_id/user_id/channel 取当前 run 上下文(缺失则空);
    agent_name 取 agent 显示名(缺失回退 run 的 agent_id/user_name)。
    """
    try:
        from storage.storage import get_storage
        storage = getattr(agent, "storage", None) or get_storage()
        if storage is None:
            return
        mcp = getattr(agent, "mcp", None)
        failed = bool(error) or _mcp_call_failed(result)
        rc = _current_run()
        user_id = getattr(rc, "user_id", "") or ""
        conv_id = getattr(rc, "conversation_id", "") or ""
        actor_id = getattr(rc, "actor_id", "") or ""
        if ":" in conv_id:
            channel = conv_id.split(":", 1)[0]
        elif ":" in user_id:
            channel = user_id.split(":", 1)[0]
        else:
            channel = ""
        if channel == "dingtalk_group":
            channel = "dingtalk"
        storage.record_mcp_call(
            server=(mcp.tool_server(exposed_name) if mcp else None) or "",
            tool=(mcp.tool_raw(exposed_name) if mcp else None) or exposed_name,
            exposed=exposed_name,
            ok=not failed,
            duration_ms=max(0, int(duration_ms or 0)),
            result_chars=len(result or ""),
            error=error or (result if failed else ""),
            conversation_id=conv_id,
            user_id=user_id,
            channel=channel,
            agent_name=getattr(agent, "name", "") or getattr(rc, "agent_id", "") or "",
            actor_id=actor_id,
            subject_id=user_id,
        )
    except Exception as e:
        logger.warning(f"MCP 调用审计写入失败(忽略): {e}")


async def execute_tool_safe(agent, name: str, args: dict) -> str:
    """带权限检查、沙箱拦截、钩子、熔断器和错误恢复的工具执行"""
    cb = _get_user_circuit_breaker(agent)
    if cb and name != "ask_user" and not cb.allow_request():
        logger.warning(f"熔断器开启，拒绝工具调用: {name}")
        return cb.get_fallback()

    perm_result = agent.permission.check(name, args)
    if not perm_result:
        logger.warning(f"工具调用被拦截: {name}, 原因: {perm_result.reason}")
        return json.dumps({"success": False, "error": perm_result.reason}, ensure_ascii=False)

    role = _current_run().session.role if _current_run().session else ""
    if agent.rbac and role:
        try:
            is_write = agent.permission.classify_access(name, args) == "write"
        except Exception:
            is_write = True  # 分类异常保守视为写操作, 只读限定条目不放行
        if not agent.rbac.check_tool(role, name, is_write=is_write):
            logger.warning(f"RBAC: 角色 [{role}] 无权执行工具 [{name}]")
            return "抱歉，您当前没有使用该功能的权限，请联系管理员开通。"
        # 代授权求交(on-behalf-of): 零号员工等服务身份代为执行时, actor 也须放行;
        # 有效权限 = actor grant ∩ subject grant。个人 Agent(actor 为空)不受影响。
        actor_role = getattr(_current_run(), "actor_role", "") or ""
        if actor_role and not agent.rbac.check_tool(actor_role, name, is_write=is_write):
            logger.warning(f"RBAC: 代授权执行者 [{actor_role}] 无权执行工具 [{name}](subject={role})")
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

    _mark_run_sensitive_if_hit(agent, name)

    try:
        result = await execute_tool(agent, name, args)

        cb = _get_user_circuit_breaker(agent)
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
    except asyncio.CancelledError:
        logger.warning(f"工具调用被取消: {name}")
        raise
    except Exception as e:
        err_text = str(e) or type(e).__name__
        logger.error(f"工具 {name} 执行失败: {err_text}")
        cb = _get_user_circuit_breaker(agent)
        if cb and name != "ask_user":
            cb.on_failure()
        return json.dumps({"success": False, "error": err_text}, ensure_ascii=False)


async def execute_tool(agent, name: str, args: dict) -> str:
    """执行工具（根据名称分发到对应的工具实现）"""
    rc = _current_run()
    current_uid = ""
    if rc.session and rc.session.user_id:
        current_uid = rc.session.user_id
    elif rc.user_id:
        current_uid = rc.user_id
    current_sid = ""
    if rc.session and getattr(rc.session, "session_id", ""):
        current_sid = rc.session.session_id

    try:
        if name == "subagent" and agent.subagent_manager:
            return await execute_subagent(agent, args)

        if agent.tool_registry and agent.tool_registry.has_tool(name):
            if name == "memory":
                # 群共享上下文: 记忆工具置空属主 → 只读 global(list)、不读/写触发人私有
                # 记忆(load_memory('') 返回空; save 因无 user_id 拒绝)，防群内串隐私。
                args["_local_user_id"] = "" if getattr(rc, "group_context", False) else current_uid
            return await agent.tool_registry.execute(name, args)

        if agent.skill_manager and name in ("skill", "execute_skill"):
            return await agent.skill_manager.execute_tool(name, args)

        if agent.mcp and agent.mcp.has_tool(name):
            # MCP 工具: 调用前后测耗时并落审计(成功/失败都记); 审计失败不影响调用。
            # has_tool 与 tool_risk 同源于 _rebuild_tool_defs 的映射表, has_tool 为真
            # 时风险必非 None(至少 unknown), 不存在"有工具但无风险"的回退分支。
            started = time.monotonic()
            try:
                result = await agent.mcp.call_tool(name, args)
            except Exception as e:
                _record_mcp_call(agent, name, int((time.monotonic() - started) * 1000), error=str(e))
                raise
            _record_mcp_call(agent, name, int((time.monotonic() - started) * 1000), result=result)
            return result

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
                        if current_sid:
                            args["_local_session_id"] = current_sid
                        return await plugin.execute_tool(name, args)

        return f"工具 {name} 不存在"
    except Exception as e:
        return f"工具执行错误: {str(e) or type(e).__name__}"


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
            # 确定性子线程 id：<当前上下文线程>#<agent>，使同一对话内子代理上下文连续、
            # 且按(对话, agent)天然隔离（不同对话/用户不复用同一实例）。
            _rc = _current_run()
            _base_sess = getattr(_rc, "session", None)
            _base_sid = ""
            if _base_sess is not None and getattr(_base_sess, "session_id", ""):
                _base_sid = _base_sess.session_id
            elif getattr(_rc, "conversation_id", ""):
                _base_sid = _rc.conversation_id
            _req_sid = (args.get("session_id") or "").strip()
            _label = (args.get("name") or args.get("template") or "sub").strip()
            if _req_sid:
                thread_id = _req_sid
            elif _base_sid:
                thread_id = f"{_base_sid}#{_label}"
            else:
                thread_id = ""
            instance, _ = await agent.subagent_manager.get_or_create_subagent(
                template=args.get("template", ""),
                name=args.get("name", ""),
                session_id=thread_id,
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
        with contextlib.suppress(Exception):
            sub_agent.hooks.unregister(event, callback)
