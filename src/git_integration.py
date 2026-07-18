"""
Git 自动管理 — 增量提交 + 检查点 + 原子回滚

设计思路（参考 grok-build）：
- 阶段完成后自动创建原子 commit（Conventional Commits 格式）
- 关键操作前创建检查点（checkpoint），失败时一键回滚
- 每个功能和 commit 一一对应
- 支持结构化 commit message 生成

用法:
    git = GitIntegration(workspace)

    # 阶段完成后自动提交
    await git.auto_commit(stage="implementation", role="代码工程师")

    # 高风险操作前创建检查点
    await git.create_checkpoint("before_refactor")

    # 失败时回滚
    await git.rollback_to_checkpoint("before_refactor")
"""
import json
import logging
import os
import subprocess
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger("agent.git_integration")


class GitIntegration:
    """Git 自动管理"""

    COMMIT_TYPES = {
        "feat": "新功能",
        "fix": "Bug 修复",
        "refactor": "代码重构",
        "test": "测试",
        "docs": "文档",
        "style": "代码格式",
        "chore": "工程配置",
        "perf": "性能优化",
        "security": "安全修复",
    }

    def __init__(self, workspace: str, agent_dir: str = ".agent"):
        self.workspace = workspace
        self.agent_dir = agent_dir
        self._checkpoints: list[dict] = []
        self._is_git = self._check_git()

    # ── 核心接口 ───────────────────────────────────────

    async def auto_commit(self, stage: str, role: str, description: str = "") -> Optional[str]:
        """阶段完成后自动创建原子提交

        Args:
            stage: 阶段标识（如 implementation, testing）
            role: 角色名（如 代码工程师）
            description: 额外的描述信息

        Returns:
            提交的 hash，无变更时返回 None
        """
        if not self._is_git:
            logger.debug("[git] 非 Git 仓库，跳过提交")
            return None

        # 检查是否有变更
        diff_stat = self._get_diff_stat()
        if not diff_stat:
            logger.debug("[git] 无变更，跳过提交")
            return None

        # 自动分类 commit 类型
        commit_type = self._classify_changes(diff_stat, stage)
        scope = self._detect_scope(diff_stat)

        # 生成 title 和 body
        title = f"{commit_type}({scope}): {stage} - {role}"
        body = self._build_commit_body(diff_stat, stage, role, description)

        # 执行提交
        try:
            subprocess.run(["git", "add", "."], cwd=self.workspace,
                          capture_output=True, text=True, timeout=30)
            result = subprocess.run(
                ["git", "commit", "-m", title, "-m", body],
                cwd=self.workspace, capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                # 获取 commit hash
                hash_result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=self.workspace, capture_output=True, text=True, timeout=10,
                )
                commit_hash = hash_result.stdout.strip()[:12]
                logger.info(f"[git] ✅ {commit_type}({scope}): {title[:60]} ({commit_hash})")
                return commit_hash
            else:
                logger.warning(f"[git] 提交失败: {result.stderr.strip()}")
                return None
        except Exception as e:
            logger.warning(f"[git] 提交异常: {e}")
            return None

    async def create_checkpoint(self, name: str) -> bool:
        """创建还原点（关键操作前调用）

        Args:
            name: 检查点名称（如 "before_refactor", "before_migration"）

        Returns:
            是否成功
        """
        if not self._is_git:
            return False

        try:
            # 获取当前状态
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.workspace, capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.workspace, capture_output=True, text=True, timeout=10,
            ).stdout.strip()

            # 先把当前改动 stash 起来
            stash_result = subprocess.run(
                ["git", "stash", "push", "-m", f"checkpoint:{name}"],
                cwd=self.workspace, capture_output=True, text=True, timeout=10,
            )
            # stash 完后立刻 pop 回来（我们的目的是记录状态，不是真 stash）
            if "No local changes" not in stash_result.stderr:
                subprocess.run(["git", "stash", "apply", "stash@{0}"],
                              cwd=self.workspace, capture_output=True, timeout=10)

            checkpoint = {
                "name": name,
                "timestamp": time.time(),
                "branch": branch,
                "head": head,
                "stash_ref": f"stash@{{{name}}}",
            }

            # 持久化到文件
            self._save_checkpoint(checkpoint)
            self._checkpoints.append(checkpoint)

            logger.info(f"[git] ✅ 检查点已创建: {name} @ {branch} ({head[:12]})")
            return True
        except Exception as e:
            logger.warning(f"[git] 检查点创建失败: {e}")
            return False

    async def rollback_to_checkpoint(self, name: str) -> bool:
        """回滚到指定检查点

        Args:
            name: 检查点名称

        Returns:
            是否成功回滚
        """
        if not self._is_git:
            return False

        checkpoint = self._find_checkpoint(name)
        if not checkpoint:
            logger.warning(f"[git] 未找到检查点: {name}")
            return False

        try:
            # 放弃当前未提交的改动
            subprocess.run(["git", "add", "."], cwd=self.workspace,
                          capture_output=True, text=True, timeout=10)

            # 硬重置到检查点的 HEAD
            head = checkpoint["head"]
            subprocess.run(
                ["git", "reset", "--hard", head],
                cwd=self.workspace, capture_output=True, text=True, timeout=30,
            )

            logger.info(f"[git] ✅ 已回滚到检查点: {name} -> {head[:12]}")
            return True
        except Exception as e:
            logger.warning(f"[git] 回滚失败: {e}")
            return False

    async def rollback_last_commit(self) -> bool:
        """回滚上一次提交（撤销最近的 commit，保留改动在工作区）"""
        if not self._is_git:
            return False
        try:
            result = subprocess.run(
                ["git", "reset", "--soft", "HEAD~1"],
                cwd=self.workspace, capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                logger.info("[git] ✅ 已撤销上一次提交（改动保留在工作区）")
                return True
            else:
                logger.warning(f"[git] 撤销提交失败: {result.stderr.strip()}")
                return False
        except Exception as e:
            logger.warning(f"[git] 撤销提交异常: {e}")
            return False

    # ── Commit 信息生成 ────────────────────────────────

    COMMIT_KEYWORDS = {
        "feat": ["新增", "添加", "实现", "支持", "增加", "feat", "feature", "add", "new", "implement"],
        "fix": ["修复", "修正", "解决", "bug", "fix", "patch", "hotfix"],
        "refactor": ["重构", "重写", "优化", "重组", "refactor", "rewrite", "restructure"],
        "test": ["测试", "test", "spec", "unittest"],
        "docs": ["文档", "注释", "doc", "readme", "api"],
        "style": ["格式", "排版", "缩进", "style", "format", "lint"],
        "perf": ["性能", "加速", "perf", "performance", "optimize"],
        "security": ["安全", "漏洞", "加密", "security", "secure", "vuln", "cve"],
    }

    def _classify_changes(self, diff_stat: list[dict], stage: str) -> str:
        """根据变更内容自动判断 commit 类型"""
        # 先按阶段推断
        stage_type_map = {
            "implementation": "feat",
            "testing": "test",
            "security": "security",
            "documentation": "docs",
            "deployment": "chore",
            "refactor": "refactor",
        }
        if stage in stage_type_map:
            return stage_type_map[stage]

        # 否则根据变更内容猜测
        all_files = " ".join(d["file"] for d in diff_stat)
        all_changes = " ".join(d.get("summary", "") for d in diff_stat)
        combined = (all_files + " " + all_changes).lower()

        # 找匹配类型
        for commit_type, keywords in self.COMMIT_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in combined:
                    return commit_type

        return "chore"

    @staticmethod
    def _detect_scope(diff_stat: list[dict]) -> str:
        """检测变更所属的作用域"""
        if not diff_stat:
            return "general"

        # 取第一个文件的顶层目录作为 scope
        first_file = diff_stat[0]["file"]
        parts = first_file.replace("\\", "/").split("/")
        if len(parts) >= 2:
            return parts[0]
        return "root"

    def _build_commit_body(self, diff_stat: list[dict], stage: str,
                           role: str, description: str = "") -> str:
        """生成详细的 commit body"""
        lines = []

        if description:
            lines.append(description)
            lines.append("")

        lines.append(f"阶段: {stage}")
        lines.append(f"角色: {role}")
        lines.append(f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
        lines.append("")

        # 变更文件列表
        if diff_stat:
            lines.append("变更文件:")
            for d in diff_stat:
                insertions = d.get("insertions", 0)
                deletions = d.get("deletions", 0)
                change_str = ""
                if insertions or deletions:
                    change_str = f" (+{insertions}/-{deletions})"
                lines.append(f"  - {d['file']}{change_str}")

        return "\n".join(lines)

    # ── 查询接口 ───────────────────────────────────────

    def get_checkpoints(self) -> list[dict]:
        """获取所有检查点"""
        if not self._checkpoints:
            self._load_checkpoints()
        return list(self._checkpoints)

    def get_recent_commits(self, count: int = 5) -> list[dict]:
        """获取最近的提交"""
        if not self._is_git:
            return []
        try:
            result = subprocess.run(
                ["git", "log", f"-{count}", "--pretty=format:%h|%s|%ar"],
                cwd=self.workspace, capture_output=True, text=True, timeout=10,
            )
            commits = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 2)
                if len(parts) == 3:
                    commits.append({
                        "hash": parts[0],
                        "message": parts[1],
                        "time": parts[2],
                    })
            return commits
        except Exception as e:
            logger.debug(f"获取提交记录失败: {e}")
            return []

    @property
    def is_available(self) -> bool:
        return self._is_git

    # ── 内部 ───────────────────────────────────────────
    _CHECKPOINT_FILE = "checkpoints.json"

    def _check_git(self) -> bool:
        git_dir = os.path.join(self.workspace, ".git")
        if not os.path.isdir(git_dir):
            return False
        try:
            subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=self.workspace, capture_output=True, text=True, timeout=5,
            )
            return True
        except Exception:
            return False

    def _get_diff_stat(self) -> list[dict]:
        """获取当前变更统计"""
        try:
            result = subprocess.run(
                ["git", "diff", "--stat"],
                cwd=self.workspace, capture_output=True, text=True, timeout=10,
            )
            if not result.stdout.strip():
                return []

            files = []
            for line in result.stdout.strip().split("\n"):
                if not line or "file changed" in line or "files changed" in line:
                    continue
                # 解析: src/main.py | 10 +++++-----
                m = __import__('re').match(r'(.+?)\s*\|\s*(\d+)\s*([+\-]+)', line)
                if m:
                    changes = m.group(3)
                    files.append({
                        "file": m.group(1).strip(),
                        "lines": int(m.group(2)),
                        "insertions": changes.count("+"),
                        "deletions": changes.count("-"),
                        "summary": line.strip(),
                    })
            return files
        except Exception as e:
            logger.debug(f"获取 diff 统计失败: {e}")
            return []

    def _save_checkpoint(self, checkpoint: dict):
        """持久化检查点到文件"""
        checkpoint_dir = os.path.join(self.workspace, self.agent_dir)
        os.makedirs(checkpoint_dir, exist_ok=True)
        cp_file = os.path.join(checkpoint_dir, self._CHECKPOINT_FILE)

        existing = []
        if os.path.exists(cp_file):
            try:
                with open(cp_file, encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = []

        existing.append(checkpoint)
        with open(cp_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)

    def _load_checkpoints(self):
        """从文件加载检查点"""
        cp_file = os.path.join(self.workspace, self.agent_dir, self._CHECKPOINT_FILE)
        if os.path.exists(cp_file):
            try:
                with open(cp_file, encoding="utf-8") as f:
                    self._checkpoints = json.load(f)
            except Exception as e:
                logger.debug(f"加载检查点失败: {e}")

    def _find_checkpoint(self, name: str) -> Optional[dict]:
        """按名称查找检查点"""
        checkpoints = self.get_checkpoints()
        for cp in checkpoints:
            if cp.get("name") == name:
                return cp
        return None
