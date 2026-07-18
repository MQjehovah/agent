"""
Agent 连接池 — 复用 Agent 实例，避免每次创建/销毁的开销。

核心思路类似数据库连接池：
- acquire(role) → 从池中取一个空闲 Agent（或创建新的）
- release(agent) → 归还到池中（不清除状态，保留上下文）
- 支持 TTL 过期、最大连接数控制、角色分类

与 SubagentManager 的关系：
- SubagentManager 负责"模板管理"（加载 PROMPT.md，创建新 Agent）
- AgentPool 负责"实例复用"（缓存活跃 Agent，避免重复初始化）
- 两者配合使用：SubagentManager 创建，AgentPool 缓存
"""
import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent import Agent

logger = logging.getLogger("agent.pool")


@dataclass
class PooledAgent:
    """池中的 Agent 包装"""
    agent: "Agent"
    role: str
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    borrow_count: int = 0
    stale_ttl: float = 300.0  # 5 分钟不活动视为过期

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.last_used) > self.stale_ttl

    @property
    def age(self) -> float:
        return time.time() - self.created_at


class AgentPool:
    """Agent 连接池

    用法:
        pool = AgentPool(subagent_manager, max_size=10)
        agent = await pool.acquire("代码工程师")
        try:
            result = await agent.run(task)
        finally:
            await pool.release(agent)
    """

    def __init__(
        self,
        subagent_manager=None,
        max_size: int = 10,
        default_ttl: float = 300.0,
        min_idle: int = 0,
    ):
        self._subagent_manager = subagent_manager
        self.max_size = max_size
        self.default_ttl = default_ttl
        self.min_idle = min_idle  # 最少保持空闲 Agent 数（预热用）

        # role -> [PooledAgent, ...]  空闲列表
        self._idle: dict[str, list[PooledAgent]] = defaultdict(list)
        # role -> [PooledAgent, ...]  借用中
        self._busy: dict[str, list[PooledAgent]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._total_created = 0
        self._cleanup_task: asyncio.Task | None = None

    # ── 核心接口 ────────────────────────────────────────

    async def acquire(
        self,
        role: str,
        team_name: str = "",
        client=None,
        parent_agent=None,
        max_iterations: int = 0,
        force_new: bool = False,
    ) -> "Agent":
        """从池中获取一个 Agent

        Args:
            role: 角色名（如 "代码工程师"、"产品经理"）
            team_name: 团队名（用于从 SubagentManager 创建）
            client: LLM 客户端
            parent_agent: 父 Agent
            max_iterations: 最大迭代次数
            force_new: 强制创建新实例（忽略池中空闲）

        Returns:
            Agent 实例
        """
        async with self._lock:
            if not force_new and self._idle.get(role):
                # 从空闲列表中取最新的（LIFO，上下文更热）
                pooled = self._idle[role].pop()
                pooled.last_used = time.time()
                pooled.borrow_count += 1
                self._busy[role].append(pooled)
                logger.debug(f"[池] 复用 {role} (已借用 {pooled.borrow_count} 次)")
                return pooled.agent

            # 检查是否达到上限
            total_for_role = len(self._idle.get(role, [])) + len(self._busy.get(role, []))
            if total_for_role >= self.max_size:
                # 尝试回收一个过期的
                await self._evict_one_stale(role)

        # 创建新 Agent
        agent = await self._create_agent(role, team_name, client, parent_agent, max_iterations)
        async with self._lock:
            pooled = PooledAgent(agent=agent, role=role, stale_ttl=self.default_ttl)
            self._busy[role].append(pooled)
            self._total_created += 1
        logger.info(f"[池] 创建新 {role} (#{self._total_created}, 池大小={self._busy_count() + self._idle_count()})")
        return agent

    async def release(self, agent: "Agent", keep_alive: bool = True):
        """归还 Agent 到池中

        Args:
            agent: 要归还的 Agent
            keep_alive: 是否保持存活（False 则直接清理）
        """
        async with self._lock:
            for role, busy_list in list(self._busy.items()):
                for i, pooled in enumerate(busy_list):
                    if pooled.agent is agent:
                        busy_list.pop(i)
                        if keep_alive and not pooled.is_stale:
                            pooled.last_used = time.time()
                            self._idle[role].append(pooled)
                            logger.debug(f"[池] 归还 {role} (空闲: {len(self._idle[role])})")
                        else:
                            reason = "过期" if pooled.is_stale else "keep_alive=False"
                            logger.debug(f"[池] 释放 {role} ({reason})")
                            # 真正清理在后台做
                            asyncio.create_task(self._cleanup_agent(pooled))
                        return
        logger.warning(f"[池] 尝试归还未知 Agent: {getattr(agent, 'name', '?')}")

    async def execute(
        self,
        task: str,
        role: str,
        team_name: str = "",
        client=None,
        parent_agent=None,
        max_iterations: int = 0,
        session_id: str = "",
    ) -> str:
        """快捷方法：acquire → run → release 一步完成"""
        agent = await self.acquire(role, team_name, client, parent_agent, max_iterations)
        try:
            from agent import AgentResult
            result = await agent.run(task, session_id=session_id,
                                      user_id="cli:admin", user_name="管理员")
            return result.result if hasattr(result, 'result') else str(result)
        finally:
            await self.release(agent)

    # ── 批量执行（真并行核心） ──────────────────────────

    async def map(
        self,
        tasks: list[dict],
        max_concurrent: int = 4,
    ) -> list[tuple[str, str]]:
        """批量提交任务到池中真并行执行

        Args:
            tasks: [{"task": str, "role": str, "team_name": str, ...}, ...]
            max_concurrent: 最大并发数

        Returns:
            [(role, result_string), ...] 顺序与输入一致
        """
        sem = asyncio.Semaphore(max_concurrent)

        async def _run_one(item: dict) -> tuple[str, str]:
            role = item.get("role", "unknown")
            async with sem:
                agent = await self.acquire(
                    role=role,
                    team_name=item.get("team_name", ""),
                    client=item.get("client"),
                    parent_agent=item.get("parent_agent"),
                    max_iterations=item.get("max_iterations", 0),
                )
                try:
                    from agent import AgentResult
                    sid = item.get("session_id", "")
                    result = await agent.run(
                        item["task"],
                        session_id=sid,
                        user_id="cli:admin",
                        user_name="管理员",
                    )
                    text = result.result if hasattr(result, 'result') else str(result)
                    return (role, text)
                finally:
                    await self.release(agent)

        tasks_to_run = list(tasks)
        results = await asyncio.gather(
            *[_run_one(t) for t in tasks_to_run],
            return_exceptions=True,
        )

        final: list[tuple[str, str]] = []
        for i, r in enumerate(results):
            role = tasks_to_run[i].get("role", "unknown")
            if isinstance(r, Exception):
                logger.error(f"[池] map 任务 {role} 异常: {r}")
                final.append((role, f"ERROR: {r}"))
            elif isinstance(r, BaseException):
                final.append((role, f"CANCELLED: {r}"))
            else:
                final.append(r)
        return final

    # ── 池管理 ─────────────────────────────────────────

    @property
    def total_created(self) -> int:
        return self._total_created

    @property
    def idle_count(self) -> int:
        return sum(len(v) for v in self._idle.values())

    @property
    def busy_count(self) -> int:
        return sum(len(v) for v in self._busy.values())

    def _idle_count(self) -> int:
        return self.idle_count

    def _busy_count(self) -> int:
        return self.busy_count

    def get_stats(self) -> dict:
        return {
            "total_created": self._total_created,
            "idle": self.idle_count,
            "busy": self.busy_count,
            "roles": {
                "idle": {k: len(v) for k, v in self._idle.items()},
                "busy": {k: len(v) for k, v in self._busy.items()},
            },
        }

    async def warmup(self, roles: list[str], team_name: str = "", count: int = 1):
        """预热：提前创建 Agent 实例"""
        for role in roles:
            for _ in range(count):
                agent = await self._create_agent(role, team_name)
                async with self._lock:
                    pooled = PooledAgent(agent=agent, role=role, stale_ttl=self.default_ttl)
                    self._idle[role].append(pooled)
                    self._total_created += 1
        logger.info(f"[池] 预热完成: {roles} x{count}")

    async def evict_all(self):
        """清理所有 Agent"""
        async with self._lock:
            all_agents = []
            for role, lst in list(self._idle.items()):
                all_agents.extend(lst)
            self._idle.clear()
            for role, lst in list(self._busy.items()):
                all_agents.extend(lst)
            self._busy.clear()
        for pooled in all_agents:
            await self._cleanup_agent(pooled)
        logger.info(f"[池] 已清除 {len(all_agents)} 个 Agent")

    async def start_cleanup_task(self, interval: int = 120):
        """启动定期清理过期 Agent 的后台任务"""
        if self._cleanup_task and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(interval))
        logger.info(f"[池] 清理任务已启动 (间隔={interval}s)")

    def stop_cleanup_task(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None

    # ── 内部 ────────────────────────────────────────────

    async def _create_agent(
        self,
        role: str,
        team_name: str = "",
        client=None,
        parent_agent=None,
        max_iterations: int = 0,
    ) -> "Agent":
        """创建新 Agent（委托给 SubagentManager）"""
        if self._subagent_manager:
            agent = await self._subagent_manager._create_team_subagent(
                team_name or self._subagent_manager._team_name or "",
                role,
                client=client,
                parent_agent=parent_agent,
                max_iterations=max_iterations,
            )
            return agent

        # 没有 SubagentManager 时直接创建
        from agent import Agent
        ws = getattr(parent_agent, 'workspace', '.')
        cfg = getattr(parent_agent, 'config_dir', '.')
        agent = Agent(
            workspace=ws,
            client=client,
            parent_agent=parent_agent,
            config_dir=cfg,
        )
        await agent.initialize()
        if max_iterations > 0:
            agent.max_iterations = max_iterations
        return agent

    async def _evict_one_stale(self, role: str):
        """回收一个过期 Agent 腾出空间"""
        for pooled in list(self._idle.get(role, [])):
            if pooled.is_stale:
                self._idle[role].remove(pooled)
                asyncio.create_task(self._cleanup_agent(pooled))
                logger.debug(f"[池] 回收过期 {role}")
                return True
        return False

    async def _cleanup_agent(self, pooled: PooledAgent):
        """清理单个 Agent"""
        try:
            await pooled.agent.cleanup()
        except Exception as e:
            logger.warning(f"[池] Agent 清理失败: {e}")

    async def _cleanup_loop(self, interval: int):
        """定期清理循环"""
        while True:
            await asyncio.sleep(interval)
            try:
                await self._evict_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[池] 清理循环异常: {e}")

    async def _evict_expired(self):
        """清理所有过期的空闲 Agent"""
        async with self._lock:
            expired: list[PooledAgent] = []
            for role, lst in list(self._idle.items()):
                still_fresh = []
                for pooled in lst:
                    if pooled.is_stale:
                        expired.append(pooled)
                    else:
                        still_fresh.append(pooled)
                if still_fresh:
                    self._idle[role] = still_fresh
                else:
                    self._idle.pop(role, None)
        for pooled in expired:
            await self._cleanup_agent(pooled)
        if expired:
            logger.info(f"[池] 清理了 {len(expired)} 个过期 Agent")
