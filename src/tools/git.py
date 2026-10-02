"""Git 工具 — 状态/差异/日志/提交/检查点/回滚，供编码 agent 使用。

- 只读操作: status / diff / log
- 写操作: commit / checkpoint / rollback（经权限层确认；rollback 为破坏性）
- 仅在 Git 仓库工作区内可用；路径参数限制在工作区内
"""
import json
import logging
import os
import subprocess

from . import BuiltinTool

logger = logging.getLogger("agent.tools")

_READ_OPS = ("status", "diff", "log")
_WRITE_OPS = ("commit", "checkpoint", "rollback")


def _run_git(args: list[str], cwd: str, timeout: int = 30) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return -1, "", "git 不可用（未安装或不在 PATH）"
    except subprocess.TimeoutExpired:
        return -1, "", f"git 命令超时({timeout}s)"
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


def is_git_repo(workspace: str) -> bool:
    if not os.path.isdir(os.path.join(workspace, ".git")):
        return False
    code, _out, _err = _run_git(["rev-parse", "--git-dir"], workspace, timeout=5)
    return code == 0


class GitTool(BuiltinTool):
    """Git 操作工具"""

    @property
    def name(self) -> str:
        return "git"

    @property
    def description(self) -> str:
        return """在 Git 仓库工作区内执行常用 Git 操作。

operation（必填）:
- status     : 查看工作区状态（简短格式 + 当前分支）
- diff       : 查看差异（staged=true 看暂存区；path 限定文件）
- log        : 查看最近提交（count 默认 10）
- commit     : 暂存并提交（message 必填；all 默认 true 暂存全部改动）
- checkpoint : 创建可回滚的检查点（name，默认 auto）
- rollback   : 回滚到检查点（name）或上一次提交（name 省略 / "last"，保留改动到工作区）

写操作（commit/checkpoint/rollback）受权限模式确认；rollback 为破坏性操作。
非 Git 仓库或 git 不可用会返回失败原因。"""

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["status", "diff", "log", "commit", "checkpoint", "rollback"],
                    "description": "要执行的操作"
                },
                "message": {"type": "string", "description": "commit 提交信息（operation=commit 时必填）"},
                "path": {"type": "string", "description": "限定文件路径（相对工作区；status/diff/commit 可用）"},
                "staged": {"type": "boolean", "description": "diff 是否查看暂存区（默认 false）", "default": False},
                "count": {"type": "integer", "description": "log 返回条数（默认 10）", "default": 10},
                "name": {"type": "string", "description": "检查点名称（checkpoint/rollback）"},
                "all": {"type": "boolean", "description": "commit 是否暂存全部改动（默认 true）", "default": True},
            },
            "required": ["operation"]
        }

    async def execute(self, **kwargs) -> str:
        operation = str(kwargs.get("operation", "") or "").strip()
        workspace = self.workspace or os.getcwd()

        if operation not in _READ_OPS + _WRITE_OPS:
            return self._error(f"不支持的 operation: {operation!r}（可选 {', '.join(_READ_OPS + _WRITE_OPS)}）")
        if not is_git_repo(workspace):
            return self._error("当前工作区不是 Git 仓库（缺少 .git 或 git 不可用）")

        if operation == "status":
            return self._status(workspace, kwargs.get("path", ""))
        if operation == "diff":
            return self._diff(workspace, kwargs)
        if operation == "log":
            return self._log(workspace, kwargs.get("count", 10))
        if operation == "commit":
            return self._commit(workspace, kwargs)
        if operation == "checkpoint":
            return await self._checkpoint(workspace, kwargs.get("name", "auto"))
        return await self._rollback(workspace, kwargs.get("name", ""))

    # ── 只读 ──────────────────────────────────────────

    def _status(self, workspace: str, path: str) -> str:
        args = ["status", "--short", "--branch"]
        if path:
            full = self.resolve_path(path)
            if not self.is_path_allowed(full):
                return self._error("路径超出工作目录范围")
            args.append("--")
            args.append(os.path.relpath(full, workspace))
        code, out, err = _run_git(args, workspace)
        if code != 0:
            return self._error(err.strip() or "git status 失败")
        return json.dumps({"success": True, "operation": "status", "output": out.strip() or "(干净)"},
                          ensure_ascii=False, indent=2)

    def _diff(self, workspace: str, kwargs: dict) -> str:
        args = ["diff"]
        if kwargs.get("staged"):
            args.append("--staged")
        path = kwargs.get("path", "")
        if path:
            full = self.resolve_path(path)
            if not self.is_path_allowed(full):
                return self._error("路径超出工作目录范围")
            args.append("--")
            args.append(os.path.relpath(full, workspace))
        code, out, err = _run_git(args, workspace, timeout=60)
        if code != 0:
            return self._error(err.strip() or "git diff 失败")
        capped = out if len(out) <= 20000 else out[:20000] + "\n…[diff 过长已截断]"
        return json.dumps({"success": True, "operation": "diff", "output": capped or "(无差异)"},
                          ensure_ascii=False, indent=2)

    def _log(self, workspace: str, count) -> str:
        try:
            n = max(1, min(int(count or 10), 100))
        except (TypeError, ValueError):
            n = 10
        code, out, err = _run_git(["log", f"-{n}", "--pretty=format:%h %ad %s", "--date=short"], workspace)
        if code != 0:
            return self._error(err.strip() or "git log 失败")
        return json.dumps({"success": True, "operation": "log", "output": out.strip() or "(无提交)"},
                          ensure_ascii=False, indent=2)

    # ── 写操作 ────────────────────────────────────────

    def _commit(self, workspace: str, kwargs: dict) -> str:
        message = str(kwargs.get("message", "") or "").strip()
        if not message:
            return self._error("operation=commit 需要提供 message")

        if kwargs.get("all", True):
            code, out, err = _run_git(["add", "-A"], workspace)
            if code != 0:
                return self._error(err.strip() or "git add 失败")
        elif kwargs.get("path"):
            full = self.resolve_path(kwargs["path"])
            if not self.is_path_allowed(full):
                return self._error("路径超出工作目录范围")
            code, out, err = _run_git(["add", "--", os.path.relpath(full, workspace)], workspace)
            if code != 0:
                return self._error(err.strip() or "git add 失败")

        code, out, err = _run_git(["commit", "-m", message], workspace)
        if code != 0:
            return self._error(err.strip() or out.strip() or "git commit 失败（可能没有变更或未配置 user.name/email）")
        code, head, _ = _run_git(["rev-parse", "--short", "HEAD"], workspace)
        return json.dumps({
            "success": True, "operation": "commit",
            "commit": head.strip() if code == 0 else "",
            "output": out.strip(),
        }, ensure_ascii=False, indent=2)

    async def _checkpoint(self, workspace: str, name: str) -> str:
        from git_integration import GitIntegration
        git = GitIntegration(workspace)
        ok = await git.create_checkpoint(name or "auto")
        if not ok:
            return self._error("创建检查点失败（非 Git 仓库或 git 异常）")
        return json.dumps({"success": True, "operation": "checkpoint", "name": name or "auto"},
                          ensure_ascii=False, indent=2)

    async def _rollback(self, workspace: str, name: str) -> str:
        from git_integration import GitIntegration
        git = GitIntegration(workspace)
        if not name or name == "last":
            ok = await git.rollback_last_commit()
            action = "已撤销上一次提交（改动保留在工作区）"
        else:
            ok = await git.rollback_to_checkpoint(name)
            action = f"已回滚到检查点 {name}"
        if not ok:
            return self._error("回滚失败（检查点不存在或 git 异常）")
        return json.dumps({"success": True, "operation": "rollback", "action": action},
                          ensure_ascii=False, indent=2)

    @staticmethod
    def _error(msg: str) -> str:
        return json.dumps({"success": False, "error": msg}, ensure_ascii=False)
