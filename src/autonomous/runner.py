import asyncio
import contextlib
import logging
import signal

from rich.panel import Panel

from autonomous.eventbus import EventBus
from autonomous.executor import Executor
from autonomous.goal import GoalManager
from autonomous.loop import AutonomousLoop
from autonomous.perceiver import Perceiver
from autonomous.planner import Planner
from autonomous.reporter import DingTalkReporter, Reporter
from autonomous.verifier import Verifier
from bootstrap import console
from storage.storage import get_storage

logger = logging.getLogger("agent.main")


async def autonomous_mode(agent, shutdown_event: asyncio.Event, args):
    """自主模式 - 感知-规划-执行-校验循环"""
    storage = get_storage()

    event_bus = EventBus(storage=storage)
    goal_manager = GoalManager(storage=storage)

    kanban_board = None
    if agent.plugin_manager:
        kp = agent.plugin_manager.get_plugin("kanban")
        if kp:
            kanban_board = kp.get_board()

    tool_summary = ""
    if hasattr(agent, "_get_tool_summary"):
        tool_summary = agent._get_tool_summary()

    subagent_summary = ""
    if agent.factory:
        subagent_summary = agent.factory.get_subagent_prompt()

    perceiver = Perceiver(event_bus=event_bus, agent=agent)
    planner = Planner(
        client=agent.client,
        tool_summary=tool_summary,
        subagent_summary=subagent_summary,
    )

    dingtalk_plugin = None
    plugin_manager = agent.plugin_manager

    if plugin_manager:
        dingtalk_plugin = plugin_manager.get_plugin("dingtalk")

        scheduler_plugin = plugin_manager.get_plugin("scheduler")
        if scheduler_plugin:
            async def _schedule_to_perceiver(schedule_task: str):
                await perceiver.handle_schedule({"name": "定时任务", "task": schedule_task})
            scheduler_plugin._agent_executor = _schedule_to_perceiver
            scheduler_plugin.start()

    if (
        dingtalk_plugin
        and hasattr(dingtalk_plugin, "sessions")
        and dingtalk_plugin.sessions
    ):
        reporter = DingTalkReporter(dingtalk_plugin=dingtalk_plugin)
    else:
        reporter = Reporter()

    executor = Executor(agent=agent, reporter=reporter)
    verifier = Verifier(client=agent.client)

    auto_loop = AutonomousLoop(
        event_bus=event_bus,
        agent=agent,
        goal_manager=goal_manager,
        planner=planner,
        executor=executor,
        verifier=verifier,
        reporter=reporter,
        perceiver=perceiver,
        board=kanban_board,
        shutdown_event=shutdown_event,
    )

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            asyncio.get_running_loop().add_signal_handler(sig, shutdown_event.set)

    board_info = ""
    if kanban_board:
        stats = kanban_board.get_stats()
        board_info = f"看板: {stats['total']} 个任务 ({stats['by_column']})"

    console.print(
        Panel.fit(
            "[bold green]自主模式已启动[/bold green]\n"
            f"目标数据库: {storage.db_path}\n"
            f"{board_info}\n"
            "信号源: 钉钉消息 | Webhook | 定时任务 | 看板\n"
            "等待事件...",
            border_style="green",
        )
    )

    await auto_loop.run()
    return plugin_manager
