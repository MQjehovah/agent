import asyncio
import contextlib
import logging

from bootstrap import bootstrap, cleanup, setup_environment
from cli import parse_args

logger = logging.getLogger("agent.main")


async def main():
    setup_environment(__file__)
    args = parse_args()
    await bootstrap(args)

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
        await cleanup()
        logger.info("清理完成")


if __name__ == "__main__":
    with contextlib.suppress(Exception):
        asyncio.run(main())
