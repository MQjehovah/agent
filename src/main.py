import asyncio
import logging

from bootstrap import bootstrap, cleanup, setup_environment
from cli import parse_args

logger = logging.getLogger("agent.main")


async def main():
    setup_environment(__file__)
    args = parse_args()
    agent, plugin_manager, web_server, target_agent = await bootstrap(args)
    shutdown_event = asyncio.Event()

    try:
        if args.mode == "autonomous":
            from autonomous.runner import autonomous_mode
            await autonomous_mode(agent, shutdown_event, args)
        else:
            from interactive import interactive_mode
            await interactive_mode(agent, shutdown_event, target_agent)
    except asyncio.CancelledError:
        logger.info("任务取消")
    except Exception as e:
        logger.error(f"程序异常退出: {e}", exc_info=True)
    finally:
        logger.info("清理资源...")
        if web_server:
            web_server.stop()
        await cleanup(plugin_manager, agent)
        logger.info("清理完成")


if __name__ == "__main__":
    asyncio.run(main())
