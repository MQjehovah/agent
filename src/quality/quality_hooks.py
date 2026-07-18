"""
预置代码质量 Hooks — 自动保障代码质量底线

设计思路（参考 grok-build 的 hooks 系统）：
- post_edit_lint: 文件修改后自动运行 ruff/eslint 格式化
- pre_commit_secret_scan: 提交前扫描密钥和敏感信息
- post_test: 测试运行后自动分析失败原因

用法:
    from quality_hooks import CodeQualityHooks
    agent.hooks.register(HookEvent.TOOL_RESULT, CodeQualityHooks.post_edit_lint)
"""
import json
import logging
import os
import re
import subprocess

logger = logging.getLogger("agent.quality_hooks")


class CodeQualityHooks:
    """预置的代码质量钩子集合"""

    # 支持自动 lint 的文件扩展名
    LINT_CONFIGS = {
        ".py": {
            "tools": [
                (["ruff", "check", "--fix", "--quiet"], "ruff"),
                (["ruff", "format", "--quiet"], "ruff_format"),
            ],
        },
        ".js": {
            "tools": [
                (["npx", "eslint", "--fix", "--quiet"], "eslint"),
            ],
        },
        ".ts": {
            "tools": [
                (["npx", "eslint", "--fix", "--quiet", "--ext", ".ts"], "eslint"),
            ],
        },
        ".json": {
            "tools": [],
        },
        ".md": {
            "tools": [],
        },
    }

    # 密钥检测模式
    SECRET_PATTERNS = [
        (r"(?i)(api[_-]?key|apikey)\s*[=:]\s*['\"](?!\{|\$)[A-Za-z0-9_\-]{16,}['\"]", "API Key"),
        (r"(?i)(secret|password|passwd|pwd)\s*[=:]\s*['\"](?!\{|\$)[A-Za-z0-9_\-!@#$%^&*()]{8,}['\"]", "Secret/Password"),
        (r"(?i)(token|auth_token|access_token|refresh_token)\s*[=:]\s*['\"](?!\{|\$)[A-Za-z0-9_\-]{16,}['\"]", "Token"),
        (r"(?i)-----BEGIN (RSA |EC )?PRIVATE KEY-----", "Private Key"),
        (r"(?i)sk-[A-Za-z0-9]{20,}", "OpenAI API Key"),
        (r"(?i)ghp_[A-Za-z0-9]{36}", "GitHub Token"),
        (r"(?i)AKIA[0-9A-Z]{16}", "AWS Access Key"),
    ]

    def __init__(self, workspace: str = ""):
        self.workspace = workspace
        self._lint_stats: dict = {"files_linted": 0, "errors_fixed": 0}

    # ── 公开钩子 ───────────────────────────────────────

    async def post_edit_lint(self, ctx):
        """文件修改后自动 lint（注册到 TOOL_RESULT 事件）

        钩子签名: async def handler(ctx)
        其中 ctx 包含 event, tool_name, arguments, result 等字段
        """
        if not hasattr(ctx, 'tool_name') or ctx.tool_name not in ("file_operation", "batch_edit", "edit"):
            return
        if not hasattr(ctx, 'arguments'):
            return

        # 获取被修改的文件路径
        file_path = ""
        if hasattr(ctx, 'arguments') and isinstance(ctx.arguments, dict):
            file_path = ctx.arguments.get("path", "")
        if not file_path and hasattr(ctx, 'arguments') and isinstance(ctx.arguments, str):
            try:
                args = json.loads(ctx.arguments)
                file_path = args.get("path", args.get("file", ""))
            except (json.JSONDecodeError, ValueError):
                pass

        if not file_path:
            return

        # 解析绝对路径
        if self.workspace and not os.path.isabs(file_path):
            file_path = os.path.join(self.workspace, file_path)

        if not os.path.isfile(file_path):
            return

        # 检查文件类型
        ext = os.path.splitext(file_path)[1].lower()
        lint_config = self.LINT_CONFIGS.get(ext)
        if not lint_config or not lint_config["tools"]:
            return

        # 执行 lint
        for cmd_args, tool_name in lint_config["tools"]:
            try:
                # 检查工具是否存在
                if tool_name == "ruff":
                    result = subprocess.run(
                        cmd_args + [file_path],
                        capture_output=True, text=True, timeout=30,
                    )
                    if result.returncode != 0 and result.stderr:
                        if "error" in result.stderr.lower() or "syntax" in result.stderr.lower():
                            logger.info(f"[quality_hooks] {tool_name} 检测到问题: {result.stderr.strip()[:200]}")
                        else:
                            logger.debug(f"[quality_hooks] {tool_name}: {result.stderr.strip()[:200]}")
                    else:
                        self._lint_stats["files_linted"] += 1
                        logger.debug(f"[quality_hooks] ✅ {tool_name} {file_path}")
            except FileNotFoundError:
                logger.debug(f"[quality_hooks] 工具 {tool_name} 未安装，跳过")
                break
            except Exception as e:
                logger.debug(f"[quality_hooks] {tool_name} 执行失败: {e}")

    async def pre_commit_secret_scan(self, ctx):
        """提交前扫描密钥（注册到 TOOL_START 事件）

        拦截包含 "git commit" 的 shell 命令，提交前扫描变更中是否包含敏感信息。
        """
        if not hasattr(ctx, 'tool_name') or ctx.tool_name != "shell":
            return
        if not hasattr(ctx, 'arguments'):
            return

        command = ""
        if isinstance(ctx.arguments, dict):
            command = ctx.arguments.get("command", "")
        elif isinstance(ctx.arguments, str):
            try:
                args = json.loads(ctx.arguments)
                command = args.get("command", "")
            except (json.JSONDecodeError, ValueError):
                command = ctx.arguments

        if "git commit" not in command.lower():
            return

        # 获取暂存区的 diff
        try:
            result = subprocess.run(
                ["git", "diff", "--cached"],
                cwd=self.workspace, capture_output=True, text=True, timeout=10,
            )
            diff = result.stdout
        except Exception as e:
            logger.debug(f"[quality_hooks] 获取暂存区 diff 失败: {e}")
            return

        if not diff:
            return

        # 扫描敏感信息
        secrets_found = []
        for i, line in enumerate(diff.split("\n"), 1):
            if line.startswith("+") and not line.startswith("+++"):
                for pattern, secret_type in self.SECRET_PATTERNS:
                    if re.search(pattern, line):
                        secrets_found.append({
                            "type": secret_type,
                            "line": i,
                            "preview": line.strip()[:80],
                        })
                        break

        if secrets_found:
            logger.warning(
                f"[quality_hooks] ⚠️ 检测到 {len(secrets_found)} 个可能的敏感信息:\n" +
                "\n".join(f"  - [{s['type']}] 第{s['line']}行: {s['preview']}" for s in secrets_found)
            )

            # 生成警告信息（通过修改 result 来阻止提交？不，我们只记录日志不阻塞）
            # hooks 系统目前不支持修改工具结果，所以只记录警告
            logger.warning("[quality_hooks] 建议在提交前移除上述敏感信息")

    async def post_test_analyze(self, ctx):
        """测试运行后分析失败原因（注册到 TOOL_RESULT 事件）

        当测试工具（pytest, jest 等）返回失败时，自动分析失败原因。
        """
        if not hasattr(ctx, 'tool_name') or ctx.tool_name != "shell":
            return
        if not hasattr(ctx, 'result'):
            return
        if not hasattr(ctx, 'arguments'):
            return

        command = ""
        if isinstance(ctx.arguments, dict):
            command = ctx.arguments.get("command", "")
        test_keywords = ["pytest", "unittest", "jest", "go test", "cargo test", "npm test"]
        if not any(kw in command.lower() for kw in test_keywords):
            return

        result_text = ctx.result if isinstance(ctx.result, str) else str(ctx.result or "")

        # 检查是否包含失败
        if "passed" in result_text.lower() and "failed" not in result_text.lower():
            return  # 全部通过，不需要分析

        failed_count = 0
        for line in result_text.split("\n"):
            if "FAILED" in line or "failed" in line:
                if "passed" not in line:  # 排除 "X passed, Y failed" 中的 failed
                    failed_count += 1

        if failed_count == 0:
            return

        logger.info(f"[quality_hooks] 检测到 {failed_count} 个测试失败，需分析原因")

    # ── 注册辅助 ───────────────────────────────────────

    def register_all(self, hooks):
        """注册所有钩子到 HookManager"""
        from hooks import HookEvent
        hooks.register(HookEvent.TOOL_RESULT, self.post_edit_lint)
        hooks.register(HookEvent.PRE_TOOL_USE, self.pre_commit_secret_scan)
        hooks.register(HookEvent.TOOL_RESULT, self.post_test_analyze)
        logger.info("[quality_hooks] 已注册 3 个代码质量钩子")

    def get_stats(self) -> dict:
        return dict(self._lint_stats)

    def get_lint_config_preview(self) -> str:
        """显示当前支持的 lint 配置"""
        lines = ["支持的自动 lint 工具:"]
        for ext, config in self.LINT_CONFIGS.items():
            tools = [t[1] for t in config["tools"]]
            if tools:
                lines.append(f"  {ext}: {', '.join(tools)}")
        return "\n".join(lines)
