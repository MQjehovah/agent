"""
Git Worktree 隔离 — 为每个 Subagent 创建独立的 Git Worktree

设计思路（参考 grok-build）：
- 每个 Subagent 在独立的 Git Worktree 中工作
- 多个 Subagent 同时写代码不会冲突
- 父仓库不会被直接修改
- 完成后生成 Diff，用户审查后合并
- 非 Git 仓库使用临时目录隔离

用法:
    mgr = WorktreeManager(workspace="/path/to/repo")
    async with mgr.isolated_worktree("代码工程师") as worktree_path:
        # 在 worktree_path 下工作
        agent = Agent(workspace=worktree_path, ...)
        await agent.run(task)
    # 退出时自动清理 worktree
"""
import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional

logger = logging.getLogger("agent.worktree")


@dataclass
class WorktreeInfo:
    """Worktree 信息"""
    path: str
    branch: str
    role: str
    created_at: float = field(default_factory=time.time)
    is_git: bool = False
    is_clean: bool = True
    commit_count: int = 0


class WorktreeManager:
    """Worktree 管理器

    支持两种模式：
    1. Git Worktree 模式 — 在 Git 仓库中创建隔离的 Worktree
    2. 临时目录模式 — 非 Git 仓库使用 tempdir 隔离
    """

    def __init__(self, workspace: str, agent_dir: str = ".agent", max_worktrees: int = 8):
        self.workspace = os.path.abspath(workspace)
        self.agent_dir = agent_dir
        self.max_worktrees = max_worktrees
        self._worktrees: dict[str, WorktreeInfo] = {}
        self._lock = asyncio.Lock()
        self._is_git = self._check_is_git()

    # ── 公共接口 ───────────────────────────────────────

    @asynccontextmanager
    async def isolated_worktree(
        self,
        role: str = "agent",
        branch_prefix: str = "agent",
    ) -> AsyncIterator[str]:
        """创建一个隔离的工作目录（上下文管理器，退出时自动清理）

        Args:
            role: 角色名（用于命名分支）
            branch_prefix: 分支名前缀

        Yields:
            隔离工作目录的绝对路径
        """
        worktree_id = str(uuid.uuid4())[:8]
        branch = f"{branch_prefix}/{role}/{worktree_id}"

        if self._is_git:
            wt_path = await self._create_git_worktree(branch, worktree_id)
        else:
            wt_path = await self._create_temp_worktree(worktree_id)

        info = WorktreeInfo(
            path=wt_path,
            branch=branch,
            role=role,
            is_git=self._is_git,
        )
        async with self._lock:
            self._worktrees[worktree_id] = info

        logger.info(f"[worktree] 创建隔离目录: {wt_path} (role={role})")
        try:
            yield wt_path
        finally:
            await self._cleanup_worktree(worktree_id)

    async def create_worktree(self, role: str = "agent", branch_prefix: str = "agent") -> str:
        """创建隔离工作目录（手动管理模式）

        返回路径，调用方需要后续调用 cleanup_worktree_by_path()
        """
        worktree_id = str(uuid.uuid4())[:8]
        branch = f"{branch_prefix}/{role}/{worktree_id}"

        if self._is_git:
            wt_path = await self._create_git_worktree(branch, worktree_id)
        else:
            wt_path = await self._create_temp_worktree(worktree_id)

        info = WorktreeInfo(
            path=wt_path,
            branch=branch,
            role=role,
            is_git=self._is_git,
        )
        async with self._lock:
            self._worktrees[worktree_id] = info

        logger.info(f"[worktree] 创建隔离目录: {wt_path}")
        return wt_path

    async def cleanup_worktree_by_path(self, path: str):
        """根据路径清理 worktree"""
        async with self._lock:
            wt_id = None
            for wid, info in self._worktrees.items():
                if info.path == path:
                    wt_id = wid
                    break
        if wt_id:
            await self._cleanup_worktree(wt_id)

    async def get_worktree_diff(self, path: str) -> str:
        """获取 worktree 中的改动 diff（仅 Git 模式）"""
        if not self._check_is_git_dir(path):
            return "[非 Git 工作目录，无法生成 diff]"
        try:
            result = subprocess.run(
                ["git", "diff", "--stat"],
                cwd=path,
                capture_output=True, text=True, timeout=10,
            )
            stat = result.stdout.strip()
            result = subprocess.run(
                ["git", "diff"],
                cwd=path,
                capture_output=True, text=True, timeout=10,
            )
            diff = result.stdout.strip()
            if not diff:
                return "(无改动)"
            return f"## 改动统计\n{stat}\n\n## Diff\n```diff\n{diff[:5000]}\n```"
        except Exception as e:
            return f"[获取 diff 失败: {e}]"

    async def commit_and_push(self, path: str, message: str) -> str:
        """在 worktree 中提交改动并推送（仅 Git 模式）

        Returns:
            提交信息或错误
        """
        if not self._check_is_git_dir(path):
            return "[非 Git 仓库，跳过提交]"
        try:
            # stage 所有改动
            subprocess.run(["git", "add", "."], cwd=path, capture_output=True, text=True, timeout=10)
            # commit
            result = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=path, capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            else:
                return f"[无提交或提交失败] {result.stderr.strip()}"
        except Exception as e:
            return f"[提交失败: {e}]"

    def get_active_worktrees(self) -> list[dict]:
        """获取活跃 worktree 列表"""
        return [
            {
                "id": wid,
                "path": info.path,
                "branch": info.branch,
                "role": info.role,
                "is_git": info.is_git,
                "created_at": info.created_at,
                "age_s": int(time.time() - info.created_at),
            }
            for wid, info in self._worktrees.items()
        ]

    @property
    def is_git(self) -> bool:
        return self._is_git

    # ── 内部实现 ───────────────────────────────────────

    def _check_is_git(self) -> bool:
        git_dir = os.path.join(self.workspace, ".git")
        if not os.path.isdir(git_dir):
            return False
        try:
            subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=self.workspace,
                capture_output=True, text=True, timeout=5,
            )
            return True
        except Exception:
            return False

    @staticmethod
    def _check_is_git_dir(path: str) -> bool:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=path, capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    async def _create_git_worktree(self, branch: str, worktree_id: str) -> str:
        """创建 Git Worktree"""
        worktrees_dir = os.path.join(self.workspace, self.agent_dir, "worktrees")
        os.makedirs(worktrees_dir, exist_ok=True)
        wt_path = os.path.join(worktrees_dir, worktree_id)

        # 检查 worktree 数量上限
        existing = self._count_git_worktrees()
        if existing >= self.max_worktrees:
            logger.warning(f"[worktree] 已达上限 ({self.max_worktrees})，使用临时目录代替")
            return await self._create_temp_worktree(worktree_id)

        try:
            # 创建新分支（基于当前 HEAD）
            subprocess.run(
                ["git", "branch", "-f", branch],
                cwd=self.workspace,
                capture_output=True, text=True, timeout=10,
            )
            # 创建 worktree
            subprocess.run(
                ["git", "worktree", "add", wt_path, branch],
                cwd=self.workspace,
                capture_output=True, text=True, timeout=30,
            )
            logger.info(f"[worktree] Git worktree 已创建: {wt_path} @ {branch}")
            return wt_path
        except Exception as e:
            logger.warning(f"[worktree] Git worktree 创建失败，回退到临时目录: {e}")
            return await self._create_temp_worktree(worktree_id)

    async def _create_temp_worktree(self, worktree_id: str) -> str:
        """创建临时目录作为隔离工作区"""
        base = os.path.join(self.workspace, self.agent_dir, "worktrees")
        os.makedirs(base, exist_ok=True)
        wt_path = os.path.join(base, worktree_id)
        os.makedirs(wt_path, exist_ok=True)

        # 复制工作区内容（排除 .git 和 agent 目录）
        await self._copy_workspace_contents(self.workspace, wt_path)
        logger.info(f"[worktree] 临时目录已创建: {wt_path}")
        return wt_path

    async def _copy_workspace_contents(self, src: str, dst: str):
        """复制工作区内容到隔离目录（排除 .git 和 .agent）"""
        import fnmatch

        exclude_patterns = {".git", self.agent_dir, "__pycache__", ".venv", ".pytest_cache",
                           ".ruff_cache", ".vscode", "node_modules", ".DS_Store"}

        def _should_exclude(name: str) -> bool:
            return any(fnmatch.fnmatch(name, p) for p in exclude_patterns)

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._sync_copy, src, dst, _should_exclude)
        except Exception as e:
            logger.warning(f"[worktree] 复制工作区失败: {e}")

    def _sync_copy(self, src: str, dst: str, exclude_fn):
        """同步复制目录"""
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if not exclude_fn(d)]
            rel = os.path.relpath(root, src)
            if rel == ".":
                target_root = dst
            else:
                target_root = os.path.join(dst, rel)
                os.makedirs(target_root, exist_ok=True)
            for f in files:
                if exclude_fn(f):
                    continue
                s = os.path.join(root, f)
                d = os.path.join(target_root, f)
                try:
                    if os.path.isfile(s):
                        shutil.copy2(s, d)
                except Exception:
                    pass

    def _count_git_worktrees(self) -> int:
        try:
            result = subprocess.run(
                ["git", "worktree", "list"],
                cwd=self.workspace,
                capture_output=True, text=True, timeout=5,
            )
            return max(0, len(result.stdout.strip().split("\n")) - 1)  # 减去主仓库行
        except Exception:
            return 0

    async def _cleanup_worktree(self, worktree_id: str):
        """清理 worktree"""
        async with self._lock:
            info = self._worktrees.pop(worktree_id, None)
        if not info:
            return

        path = info.path
        if not os.path.exists(path):
            return

        logger.info(f"[worktree] 清理: {path} (role={info.role})")

        if info.is_git:
            try:
                # 先尝试 Git 方式移除
                subprocess.run(
                    ["git", "worktree", "remove", "--force", path],
                    cwd=self.workspace,
                    capture_output=True, text=True, timeout=30,
                )
                # 删除分支
                branch = info.branch
                if branch:
                    subprocess.run(
                        ["git", "branch", "-D", branch],
                        cwd=self.workspace,
                        capture_output=True, text=True, timeout=10,
                    )
            except Exception:
                pass

        # 确保目录被删除
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except Exception as e:
            logger.warning(f"[worktree] 目录清理失败: {e}")

    async def cleanup_all(self):
        """清理所有 worktree"""
        async with self._lock:
            wt_ids = list(self._worktrees.keys())
        for wid in wt_ids:
            await self._cleanup_worktree(wid)
        logger.info(f"[worktree] 已清理 {len(wt_ids)} 个 worktree")


# 兼容旧接口的快捷函数
def create_worktree_manager(workspace: str) -> WorktreeManager:
    return WorktreeManager(workspace)
