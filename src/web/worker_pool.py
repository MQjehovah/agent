"""Web 多用户 Worker 池 — 每个登录用户一个独立 Agent 实例。

背景：公司级在线 Agent 由所有员工共用。若所有 web 会话都跑在同一个 root Agent
实例上，不同用户会共享该实例的 tracer/状态与 workspace（文件工具互相可见、
实例级上下文串扰）。本池把每个用户隔离到一个独立 Agent worker：

- worker.workspace = <root workspace>/users/u_{uid}    → 文件/沙箱按人隔离
- worker 拥有独立 session_manager / subagent_manager / tracer / hooks
- 同一用户多会话由该用户 worker 顺序/并发承担；不同用户之间实例互不可见
- worker.parent_agent = root：继承 root 的 storage/LLM 客户端/插件管理器，
  但不继承上下文；persist_session=True 使 worker 保留会话历史跨轮次

容量与回收：
- max_workers = env AGENT_WEB_POOL_SIZE（0 表示不启用，退化为 root 单实例）
- 空闲超时 idle_ttl 自动清理（agent.cleanup 释放 MCP/temp/session）
- worker 被回收后再次承接旧会话时，core.run 会从 DB 恢复历史上下文
"""

import asyncio
import logging
import os
import re
import time

logger = logging.getLogger("agent.web.pool")


class PoolBusyError(RuntimeError):
    """worker 池已达上限且无可回收空闲实例（用于向请求端返回 503/提示重试）。"""


class WebUserWorkerPool:
    def __init__(self, root_agent, max_workers: int = 0, idle_ttl: float = 3600.0,
                 sweep_interval: float = 300.0, overflow: int | None = None,
                 acquire_timeout: float = 20.0):
        self.root = root_agent
        self.max_workers = max(0, int(max_workers))
        self.idle_ttl = idle_ttl
        self.sweep_interval = sweep_interval
        # 允许的临时扩容上限(超出 max_workers 的部分)。
        # None=默认 max_workers(硬顶 2x)；0=不许溢出(硬顶=max_workers)
        self.overflow = int(overflow) if overflow is not None else self.max_workers
        if self.overflow < 0:
            self.overflow = 0
        self.acquire_timeout = acquire_timeout
        # tag("web:3") -> info
        self._workers: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._total_created = 0
        self._sweep_task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self.max_workers > 0 and self.root is not None

    @staticmethod
    def _safe_dir(uid: str) -> str:
        return re.sub(r"[^0-9A-Za-z_-]", "_", str(uid)) or "x"

    async def acquire(self, tag: str, uid: str, name: str = ""):
        """为某个用户获取 worker（池未启用时返回 root）。饱和时短暂等待，仍无空闲则抛 PoolBusyError。"""
        if not self.enabled:
            return self.root
        safe = WebUserWorkerPool._safe_dir(uid)
        ws_dir = os.path.join(self.root.workspace, "users", f"u_{safe}")

        # 1) 已有该用户 worker：直接复用（同用户多会话可共用一个实例）
        async with self._lock:
            info = self._workers.get(tag)
            if info is not None and info.get("agent") is not None:
                info["busy"] += 1
                info["last"] = time.time()
                return info["agent"]

        # 2) 需要新建：先确保容量(等待可回收/空闲)，避免无界扩容撑爆内存
        if not await self._ensure_slot():
            raise PoolBusyError(
                f"系统繁忙: 并发 worker 已达上限 {self.max_workers}(可溢出 {self.overflow})，请稍后重试")

        # 3) 锁外做重量级初始化，避免阻塞其它用户的 release/acquire
        agent = await self._create_worker(tag, ws_dir)
        async with self._lock:
            info = self._workers.get(tag)
            if info is not None and info.get("agent") is not None:
                # 极端竞争(同 tag 双请求)下复用已建实例
                info["busy"] += 1
                info["last"] = time.time()
                return info["agent"]
            self._workers[tag] = {
                "agent": agent,
                "ws": ws_dir,
                "created": time.time(),
                "last": time.time(),
                "busy": 1,
            }
            self._total_created += 1
            logger.info(f"[pool] 为用户 {tag} 创建独立 worker (总 {self._total_created})")
        return agent

    async def _ensure_slot(self) -> bool:
        """容量检查：未到硬顶或可回收空闲 worker 则放行；否则等待 release 通知，超时返回 False。"""
        deadline = time.monotonic() + self.acquire_timeout
        while True:
            async with self._lock:
                if len(self._workers) < self.max_workers + self.overflow:
                    return True
                if self._evict_one_idle_locked():
                    return True
                all_busy = all(i["busy"] > 0 for i in self._workers.values())
                if not all_busy:
                    return True
                if time.monotonic() >= deadline:
                    return False
            await asyncio.sleep(0.2)

    def release(self, tag: str):
        if not self.enabled:
            return
        info = self._workers.get(tag)
        if info:
            info["busy"] = max(0, info["busy"] - 1)
            info["last"] = time.time()

    async def start(self):
        if self.enabled and self._sweep_task is None:
            self._sweep_task = asyncio.create_task(self._sweep_loop())
            logger.info(f"[pool] Worker 池启动: 容量={self.max_workers}, "
                        f"idle_ttl={self.idle_ttl}s, workspace根={self.root.workspace}")

    async def stop(self):
        if self._sweep_task:
            self._sweep_task.cancel()
            self._sweep_task = None
        await self.evict_all()

    async def _create_worker(self, tag: str, ws_dir: str):
        os.makedirs(ws_dir, exist_ok=True)
        from agent.core import Agent
        permission = getattr(self.root, "_permission_config", None)
        mode = "auto"
        try:
            if permission is not None:
                mode = permission.mode.value
        except Exception:
            mode = "auto"
        worker = Agent(
            workspace=ws_dir,
            client=self.root.client,
            parent_agent=self.root,
            permission_mode=mode,
            config_dir=getattr(self.root, "config_dir", ""),
        )
        worker.persist_session = True  # 跨轮次保留会话历史（恢复/续聊）
        worker.plugin_manager = getattr(self.root, "plugin_manager", None)
        if getattr(self.root, "name", ""):
            worker.name = self.root.name
        await worker.initialize()
        return worker

    def _evict_one_idle_locked(self):
        """容量不足时回收最久未使用的空闲 worker；无空闲则返回 False。"""
        best_tag, best_info = None, None
        for t, info in self._workers.items():
            if info["busy"] == 0 and (best_info is None or info["last"] < best_info["last"]):
                best_tag, best_info = t, info
        if best_tag is None:
            return False
        self._workers.pop(best_tag)
        asyncio.create_task(best_info["agent"].cleanup())
        logger.info(f"[pool] 容量回收 worker {best_tag}")
        return True

    async def _sweep_loop(self):
        while True:
            await asyncio.sleep(self.sweep_interval)
            try:
                await self._sweep_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[pool] 清理循环异常: {e}")

    async def _sweep_expired(self):
        now = time.time()
        expired = []
        async with self._lock:
            for t, info in list(self._workers.items()):
                if info["busy"] == 0 and (now - info["last"]) >= self.idle_ttl:
                    expired.append((t, info))
            for t, _ in expired:
                self._workers.pop(t, None)
        for t, info in expired:
            try:
                await info["agent"].cleanup()
                logger.info(f"[pool] 空闲超时回收 worker {t}")
            except Exception as e:
                logger.warning(f"[pool] 回收 worker {t} 失败: {e}")

    async def evict_all(self):
        async with self._lock:
            items = list(self._workers.items())
            self._workers.clear()
        for t, info in items:
            try:
                await info["agent"].cleanup()
            except Exception as e:
                logger.warning(f"[pool] 关闭 worker {t} 失败: {e}")
        if items:
            logger.info(f"[pool] 已清理 {len(items)} 个 worker")

    def stats(self) -> dict:
        tags = sorted(self._workers.keys())
        return {
            "enabled": self.enabled,
            "capacity": self.max_workers,
            "overflow": self.overflow,
            "hard_cap": self.max_workers + self.overflow,
            "active": len(self._workers),
            "busy": sum(1 for i in self._workers.values() if i["busy"] > 0),
            "total_created": self._total_created,
            "users": tags,
        }
