import asyncio
import logging
import threading
from typing import Coroutine, TypeVar

logger = logging.getLogger("agent.utils.async_bridge")

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_ready = threading.Event()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread, _loop_ready

    if _loop is not None and _loop.is_running():
        return _loop

    _loop_ready.clear()
    _loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(_loop)
        _loop_ready.set()
        _loop.run_forever()

    _loop_thread = threading.Thread(target=_run, daemon=True)
    _loop_thread.start()
    _loop_ready.wait(timeout=10)
    return _loop


def run_async(coro: Coroutine) -> T:
    """在任意线程中安全地运行 async 协程。

    三种上下文自动适配:
    1. 主线程且有运行中的事件循环 → asyncio.run_coroutine_threadsafe
    2. 工作线程（无事件循环） → 使用持久化事件循环线程
    3. 已在 async 上下文中 → 直接 await（不应走到这里，会抛错）
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=300)

    persistent_loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, persistent_loop)
    return future.result(timeout=300)


def shutdown():
    global _loop
    if _loop is not None and _loop.is_running():
        _loop.call_soon_threadsafe(_loop.stop)
