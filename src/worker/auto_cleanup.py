"""
资源自动清理系统 — 防止资源泄漏

设计思路（参考 grok-build 的自动清理机制）：
- 每种资源有独立的 TTL 和清理策略
- Checkpoint: 1 小时后清理
- Worktree: 2 小时后清理
- 空闲 Agent: 1 小时后清理
- 临时文件: 24 小时后清理
- 快照: 1 小时后清理

用法:
    cleanup = AutoCleanup(workspace)
    cleanup.register("checkpoints", CheckpointCleanup(ttl=3600))
    cleanup.register("worktrees", WorktreeCleanup(ttl=7200))
    await cleanup.run_cycle()  # 运行一次清理
    cleanup.start(interval=600)  # 每 10 分钟自动清理
"""
import asyncio
import logging
import os
import shutil
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("agent.auto_cleanup")


@dataclass
class CleanupResult:
    """清理结果"""
    resource_type: str
    cleaned: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


class BaseCleanup(ABC):
    """清理策略基类"""

    def __init__(self, ttl: int, name: str = ""):
        self.ttl = ttl
        self.name = name or self.__class__.__name__

    @abstractmethod
    async def clean(self) -> CleanupResult:
        """执行一次清理，返回清理结果"""


class CheckpointCleanup(BaseCleanup):
    """Git 检查点清理"""

    def __init__(self, workspace: str, ttl: int = 3600):
        super().__init__(ttl=ttl, name="checkpoints")
        self.workspace = workspace
        self._checkpoint_dir = os.path.join(workspace, ".agent")

    async def clean(self) -> CleanupResult:
        result = CleanupResult(resource_type=self.name)
        if not os.path.isdir(self._checkpoint_dir):
            return result

        cp_file = os.path.join(self._checkpoint_dir, "checkpoints.json")
        if not os.path.isfile(cp_file):
            return result

        try:
            import json
            with open(cp_file, encoding="utf-8") as f:
                checkpoints = json.load(f)

            now = time.time()
            fresh = []
            for cp in checkpoints:
                age = now - cp.get("timestamp", 0)
                if age > self.ttl:
                    result.cleaned += 1
                else:
                    fresh.append(cp)

            with open(cp_file, "w", encoding="utf-8") as f:
                json.dump(fresh, f)

            logger.info(f"[cleanup] 检查点: 清理 {result.cleaned}/{len(checkpoints)} 个")
        except Exception as e:
            result.errors.append(str(e))

        return result


class WorktreeCleanup(BaseCleanup):
    """Worktree 目录清理"""

    def __init__(self, workspace: str, ttl: int = 7200):
        super().__init__(ttl=ttl, name="worktrees")
        self.workspace = workspace
        self._worktree_dir = os.path.join(workspace, ".agent", "worktrees")

    async def clean(self) -> CleanupResult:
        result = CleanupResult(resource_type=self.name)
        if not os.path.isdir(self._worktree_dir):
            return result

        for name in os.listdir(self._worktree_dir):
            path = os.path.join(self._worktree_dir, name)
            if not os.path.isdir(path):
                continue
            try:
                age = time.time() - os.path.getmtime(path)
                if age > self.ttl:
                    shutil.rmtree(path, ignore_errors=True)
                    result.cleaned += 1
                    logger.debug(f"[cleanup] 清理 worktree: {path}")
                else:
                    result.skipped += 1
            except Exception as e:
                result.errors.append(str(e))

        if result.cleaned > 0:
            logger.info(f"[cleanup] worktree: 清理 {result.cleaned}, 跳过 {result.skipped}")
        return result


class SnapshotCleanup(BaseCleanup):
    """/undo 快照清理"""

    def __init__(self, workspace: str, ttl: int = 3600):
        super().__init__(ttl=ttl, name="snapshots")
        self.workspace = workspace
        self._snapshot_dir = os.path.join(workspace, ".agent", "snapshots")

    async def clean(self) -> CleanupResult:
        result = CleanupResult(resource_type=self.name)
        if not os.path.isdir(self._snapshot_dir):
            return result

        for name in os.listdir(self._snapshot_dir):
            path = os.path.join(self._snapshot_dir, name)
            if not os.path.isdir(path) or name == "undo_index.json":
                continue
            try:
                age = time.time() - os.path.getmtime(path)
                if age > self.ttl:
                    shutil.rmtree(path, ignore_errors=True)
                    result.cleaned += 1
            except Exception as e:
                result.errors.append(str(e))

        if result.cleaned > 0:
            logger.info(f"[cleanup] 快照: 清理 {result.cleaned} 个")
        return result


class TempDirCleanup(BaseCleanup):
    """临时目录清理（24 小时以上的 temp dir）"""

    def __init__(self, workspace: str, ttl: int = 86400):
        super().__init__(ttl=ttl, name="temp_dirs")
        self.workspace = workspace
        self._temp_root = os.path.join(workspace, ".agent")

    async def clean(self) -> CleanupResult:
        result = CleanupResult(resource_type=self.name)
        if not os.path.isdir(self._temp_root):
            return result

        import tempfile
        system_temp = tempfile.gettempdir()
        for name in os.listdir(system_temp):
            if name.startswith("agent_"):
                path = os.path.join(system_temp, name)
                try:
                    if os.path.isdir(path):
                        age = time.time() - os.path.getmtime(path)
                        if age > self.ttl:
                            shutil.rmtree(path, ignore_errors=True)
                            result.cleaned += 1
                except Exception:
                    pass

        if result.cleaned > 0:
            logger.info(f"[cleanup] 临时目录: 清理 {result.cleaned} 个")
        return result


class AutoCleanup:
    """自动清理调度器

    用法:
        cleanup = AutoCleanup(workspace)
        cleanup.register(CheckpointCleanup(workspace))   # 1h
        cleanup.register(WorktreeCleanup(workspace))     # 2h
        cleanup.register(SnapshotCleanup(workspace))     # 1h
        cleanup.register(TempDirCleanup(workspace))      # 24h
        cleanup.start(interval=600)  # 每 10 分钟检查
    """

    def __init__(self, workspace: str = ""):
        self.workspace = workspace
        self._cleaners: list[BaseCleanup] = []
        self._task: Optional[asyncio.Task] = None
        self._results: list[CleanupResult] = []

    def register(self, cleaner: BaseCleanup):
        self._cleaners.append(cleaner)
        logger.info(f"[cleanup] 注册: {cleaner.name} (TTL={cleaner.ttl}s)")

    def create_defaults(self):
        """注册所有默认清理策略"""
        self.register(CheckpointCleanup(self.workspace))
        self.register(WorktreeCleanup(self.workspace))
        self.register(SnapshotCleanup(self.workspace))
        self.register(TempDirCleanup(self.workspace))

    async def run_cycle(self) -> list[CleanupResult]:
        """运行一次完整的清理周期"""
        results = []
        for cleaner in self._cleaners:
            try:
                result = await cleaner.clean()
                results.append(result)
            except Exception as e:
                logger.warning(f"[cleanup] {cleaner.name} 异常: {e}")
                results.append(CleanupResult(
                    resource_type=cleaner.name,
                    errors=[str(e)],
                ))
        self._results.extend(results)
        return results

    def start(self, interval: int = 600):
        """启动周期性清理后台任务"""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(interval))
        logger.info(f"[cleanup] 已启动 (间隔={interval}s)")

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
            logger.info("[cleanup] 已停止")

    async def _loop(self, interval: int):
        while True:
            await asyncio.sleep(interval)
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[cleanup] 循环异常: {e}")

    def get_stats(self) -> dict:
        """获取清理统计"""
        total_cleaned = sum(r.cleaned for r in self._results)
        total_errors = sum(len(r.errors) for r in self._results)
        return {
            "cleaners": len(self._cleaners),
            "cycles_run": len(self._results) // max(len(self._cleaners), 1),
            "total_cleaned": total_cleaned,
            "total_errors": total_errors,
            "recent": [
                {"type": r.resource_type, "cleaned": r.cleaned, "errors": len(r.errors)}
                for r in self._results[-20:]
            ],
        }
