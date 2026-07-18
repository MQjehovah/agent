"""
.agentignore — 文件排除系统

控制 Agent 扫描/修改哪些文件，避免读到无关文件浪费上下文。
类似 .gitignore 语法，支持 .agentignore 和 .grokignore 两种文件名。

用法:
    ai = AgentIgnore(workspace)

    # 在工具中使用
    if ai.should_ignore(path):
        return  # 跳过这个文件

    # 注入到现有的 glob/grep 工具中
    ai.inject_into(tool_registry)
"""
import fnmatch
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("agent.agent_ignore")


class AgentIgnore:
    """.agentignore 解析器"""

    # 内置默认排除规则（防止核心文件被误改或上下文被撑爆）
    DEFAULT_PATTERNS = [
        # 依赖目录
        "node_modules/**",
        ".venv/**",
        "venv/**",
        "__pycache__/**",
        "*.pyc",
        "*.pyo",
        ".pytest_cache/**",
        ".ruff_cache/**",
        # 构建产物
        "dist/**",
        "build/**",
        "*.egg-info/**",
        ".next/**",
        # 版本控制
        ".git/**",
        ".git/*",
        ".svn/**",
        # IDE
        ".vscode/**",
        ".idea/**",
        "*.swp",
        "*.swo",
        # Agent 自身文件
        ".agent/**",
        ".grok/**",
        ".claude/**",
        # 环境变量和密钥
        ".env",
        ".env.*",
        "*.pem",
        "*.key",
        # 大文件（超过 1MB 的文件会被排除）
        "*.log",
        "*.bin",
        "*.exe",
        "*.dll",
        "*.so",
        "*.dylib",
        "*.tar.gz",
        "*.zip",
        "*.7z",
        "*.rar",
        # 图片/视频
        "*.png",
        "*.jpg",
        "*.jpeg",
        "*.gif",
        "*.ico",
        "*.mp4",
        "*.avi",
        # 数据库
        "*.db",
        "*.sqlite",
        "*.sqlite3",
    ]

    def __init__(self, workspace: str = ""):
        self.workspace = workspace
        self._patterns: list[str] = []
        self._negations: list[str] = []  # 以 ! 开头的否定规则
        self._loaded = False
        self._load()

    def _load(self):
        """从多个位置加载 .agentignore / .grokignore

        加载顺序（后加载的优先级更高）:
        1. 内置默认规则
        2. 用户目录规则 ~/.agentignore
        3. 项目级 .agentignore
        4. 项目级 .grokignore
        """
        self._patterns = list(self.DEFAULT_PATTERNS)
        self._negations = []

        # 加载顺序
        ignore_files = []
        # 全局
        home_ignore = os.path.join(os.path.expanduser("~"), ".agentignore")
        if os.path.isfile(home_ignore):
            ignore_files.append(home_ignore)
        # 项目级（优先 .agent/ 子目录，兼容工作空间根目录）
        if self.workspace:
            for fname in (".agentignore", ".grokignore"):
                for sub in (".agent", "."):
                    fpath = os.path.join(self.workspace, sub, fname)
                    if os.path.isfile(fpath):
                        ignore_files.append(fpath)
                        break

        for fpath in ignore_files:
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("!"):
                            self._negations.append(line[1:])
                        else:
                            self._patterns.append(line)
                logger.debug(f"[agentignore] 加载: {fpath}")
            except Exception as e:
                logger.warning(f"[agentignore] 加载失败 {fpath}: {e}")

        self._loaded = True
        logger.info(f"[agentignore] 已加载 {len(self._patterns)} 条排除规则, {len(self._negations)} 条例外规则")

    def should_ignore(self, path: str) -> bool:
        """判断路径是否应该被排除

        Args:
            path: 绝对路径或相对于 workspace 的路径

        Returns:
            True=应排除, False=应包含
        """
        if not self._loaded:
            self._load()

        # 转为相对路径（相对于 workspace）
        rel = self._to_relative(path)
        if not rel:
            return False

        # 检查否定规则（优先通过）
        for pattern in self._negations:
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(os.path.basename(rel), pattern):
                return False

        # 检查排除规则
        for pattern in self._patterns:
            if fnmatch.fnmatch(rel, pattern):
                return True
            # 也检查纯文件名匹配
            if fnmatch.fnmatch(os.path.basename(rel), pattern):
                return True
            # 检查目录前缀（如 __pycache__ 匹配任何深度的该目录）
            if "/" not in pattern and "/" in rel:
                parts = rel.replace("\\", "/").split("/")
                if any(fnmatch.fnmatch(part, pattern) for part in parts):
                    return True

        return False

    def filter_files(self, files: list[str]) -> list[str]:
        """过滤文件列表，只保留不应排除的文件"""
        return [f for f in files if not self.should_ignore(f)]

    def filter_tool_results(self, tool_name: str, result: str) -> str:
        """过滤工具结果中的文件路径（用于 glob 等返回文件列表的工具）"""
        if not result:
            return result

        lines = result.split("\n")
        filtered = [l for l in lines if not self.should_ignore(l.strip())]
        if len(filtered) != len(lines):
            logger.debug(f"[agentignore] {tool_name}: 过滤了 {len(lines) - len(filtered)}/{len(lines)} 个文件")
        return "\n".join(filtered)

    def inject_into(self, tool_registry):
        """注入到现有工具系统中

        修改 glob 和 grep 工具，让它们在返回结果前自动过滤排除文件。
        """
        if not tool_registry:
            return

        ai = self  # 闭包引用

        # 注入 glob
        glob_tool = tool_registry.get_tool("glob")
        if glob_tool and hasattr(glob_tool, '_original_execute'):
            return
        if glob_tool and hasattr(glob_tool, 'execute'):
            _glob_original = glob_tool.execute
            async def patched_glob(**kwargs):
                result = await _glob_original(**kwargs)
                return ai.filter_tool_results("glob", result)
            glob_tool.execute = patched_glob

        # 注入 grep
        grep_tool = tool_registry.get_tool("grep")
        if grep_tool and hasattr(grep_tool, 'execute'):
            _grep_original = grep_tool.execute
            async def patched_grep(**kwargs):
                result = await _grep_original(**kwargs)
                return ai.filter_tool_results("grep", result)
            grep_tool.execute = patched_grep

        logger.info("[agentignore] 已注入到 glob/grep 工具")

    def _to_relative(self, path: str) -> str:
        """将路径转为相对于 workspace 的路径"""
        if not path:
            return ""
        path = path.replace("\\", "/")
        if not self.workspace:
            return os.path.basename(path)
        ws = self.workspace.replace("\\", "/")
        if path.startswith(ws):
            rel = path[len(ws):].lstrip("/")
            return rel if rel else os.path.basename(path)
        return os.path.basename(path)

    def get_stats(self) -> dict:
        return {
            "patterns": len(self._patterns),
            "negations": len(self._negations),
            "loaded": self._loaded,
        }

    @staticmethod
    def generate_example(workspace: str):
        """生成示例 .agentignore 文件"""
        content = """# .agentignore — 控制 Agent 扫描/修改哪些文件
# 语法与 .gitignore 相同

# 依赖目录（避免 Agent 读到几十万行第三方代码撑爆上下文）
node_modules/
.venv/
venv/
__pycache__/
*.pyc

# 构建产物
dist/
build/
*.egg-info/

# 版本控制和 IDE
.git/
.vscode/
.idea/

# Agent 自身文件
.agent/
.grok/

# 密钥和敏感文件
.env
.env.*
*.pem
*.key

# 大文件（避免浪费 token）
*.log
*.bin
*.zip
*.tar.gz
*.png
*.jpg

# 例外：如果想允许某些被排除的文件，用 ! 开头
# !.env.example
"""
        agent_dir = os.path.join(workspace, ".agent")
        os.makedirs(agent_dir, exist_ok=True)
        path = os.path.join(agent_dir, ".agentignore")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"[agentignore] 示例文件已创建: {path}")
        return path
