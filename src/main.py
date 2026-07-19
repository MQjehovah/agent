import asyncio
import logging

from agent.factory import AgentFactory
from bootstrap import bootstrap, cleanup, setup_environment
from cli import parse_args

logger = logging.getLogger("agent.main")


async def main():
    setup_environment(__file__)
    args = parse_args()
    plugin_manager, web_server = await bootstrap(args)

    try:
        if args.mode == "autonomous":
            from autonomous.runner import autonomous_mode
            await autonomous_mode()
        else:
            from interactive import interactive_mode
            await interactive_mode()
    except asyncio.CancelledError:
        logger.info("任务取消")
    except Exception as e:
        logger.exception(f"程序异常退出: {e}")
    finally:
        logger.info("清理资源...")
        if web_server:
            web_server.stop()
        try:
            agent = await AgentFactory.instance().get_or_create(args.agent or "")
        except Exception:
            logger.warning("清理时 AgentFactory 不可用，跳过 agent cleanup")
            agent = None
        await cleanup(plugin_manager, agent)
        logger.info("清理完成")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        pass  # 已由 main() 记录日志
