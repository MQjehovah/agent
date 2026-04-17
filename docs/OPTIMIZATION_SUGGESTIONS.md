# 项目优化建议

> 基于 [OpenHarness](https://github.com/HKUDS/OpenHarness) 项目的最佳实践，对本项目提出的改进建议。

---

## 目录

- [P0 - 紧急](#p0---紧急)
  - [1. 测试基础设施](#1-测试基础设施)
  - [2. 权限系统](#2-权限系统)
- [P1 - 重要](#p1---重要)
  - [3. Pydantic 工具验证](#3-pydantic-工具验证)
  - [4. 上下文压缩](#4-上下文压缩)
  - [5. 并行工具执行](#5-并行工具执行)
- [P2 - 改进](#p2---改进)
  - [6. 钩子系统 (Hooks)](#6-钩子系统-hooks)
  - [7. 多 Provider 支持](#7-多-provider-支持)
  - [8. Token 与成本追踪](#8-token-与成本追踪)
- [P3 - 增强](#p3---增强)
  - [9. 非 CLI 输出模式](#9-非-cli-输出模式)
  - [10. 插件系统增强](#10-插件系统增强)
  - [11. CI/CD 配置](#11-cicd-配置)
  - [12. MCP HTTP 传输](#12-mcp-http-传输)
- [P0 - 工具能力补强](#p0---工具能力补强)
  - [13. 工具能力全景对比与缺口分析](#13-工具能力全景对比与缺口分析)
  - [14. 新增文件搜索工具 — GrepTool](#14-新增文件搜索工具--greptool)
  - [15. 新增文件查找工具 — GlobTool](#15-新增文件查找工具--globtool)
  - [16. 新增行级文件编辑工具 — EditTool](#16-新增行级文件编辑工具--edittool)
  - [17. 新增 Web 搜索工具 — WebSearchTool](#17-新增-web-搜索工具--websearchtool)
  - [18. 新增后台任务工具 — TaskTool](#18-新增后台任务工具--tasktool)
  - [19. 新增用户交互工具 — AskUserTool](#19-新增用户交互工具--askusertool)
  - [20. 现有工具的能力增强](#20-现有工具的能力增强)
- [P1 - Agent 核心循环优化](#p1---agent-核心循环优化)
  - [21. LLMClient 同步阻塞问题](#21-llmclient-同步阻塞问题)
  - [22. Agent 循环中 _think 的同步调用](#22-agent-循环中-_think-的同步调用)
  - [23. Agent 错误恢复](#23-agent-错误恢复)
  - [24. 流式响应支持](#24-流式响应支持)
- [P2 - 可观测性与质量](#p2---可观测性与质量)
  - [25. 结构化调用链路追踪](#25-结构化调用链路追踪)
- [附录：目录结构对比](#附录目录结构对比)

---

## P0 - 紧急

### 1. 测试基础设施

**问题：** 项目完全没有测试，任何改动都有回归风险。

**参考：** OpenHarness 拥有 114 个单元/集成测试 + 多套 E2E 测试 + CI 自动化。

**建议目录结构：**

```
tests/
├── conftest.py                 # 公共 fixtures
├── unit/
│   ├── test_tools.py           # 工具单元测试
│   ├── test_tool_registry.py   # 工具注册表测试
│   ├── test_llm.py             # LLM 客户端测试（mock API）
│   ├── test_subagent.py        # 子代理管理测试
│   ├── test_memory.py          # 记忆系统测试
│   ├── test_storage.py         # 存储层测试
│   ├── test_cache.py           # 缓存系统测试
│   ├── test_config.py          # 配置验证测试
│   └── test_skills.py          # 技能加载测试
├── integration/
│   ├── test_agent_loop.py      # Agent 主循环集成测试
│   ├── test_mcp.py             # MCP 服务器集成测试
│   └── test_plugin.py          # 插件生命周期测试
└── e2e/
    └── test_full_workflow.py   # 端到端工作流测试
```

**示例代码：**

```python
# tests/conftest.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.agent import Agent
from src.llm import LLMClient
from src.storage import Storage

@pytest.fixture
def mock_llm_client():
    client = MagicMock(spec=LLMClient)
    client.chat = AsyncMock(return_value=MagicMock(
        choices=[MagicMock(message=MagicMock(
            content="测试回复",
            tool_calls=None
        ))]
    ))
    return client

@pytest.fixture
def storage(tmp_path):
    db_path = tmp_path / "test.db"
    s = Storage(str(db_path))
    yield s
    s.close()

@pytest.fixture
def agent(tmp_path, mock_llm_client):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Agent(workspace=str(workspace), llm_client=mock_llm_client)
```

```python
# tests/unit/test_tools.py
import pytest
from src.tools.file import FileTool
from src.tools.shell import ShellTool
from src.tools.todo import TodoTool

class TestFileTool:
    def test_read_file(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world", encoding="utf-8")

        tool = FileTool()
        result = tool.execute({
            "operation": "read",
            "path": str(test_file)
        })
        assert result["success"] is True
        assert "hello world" in result["content"]

    def test_read_nonexistent_file(self):
        tool = FileTool()
        result = tool.execute({
            "operation": "read",
            "path": "/nonexistent/file.txt"
        })
        assert result["success"] is False

    def test_write_file(self, tmp_path):
        test_file = tmp_path / "output.txt"
        tool = FileTool()
        result = tool.execute({
            "operation": "write",
            "path": str(test_file),
            "content": "new content"
        })
        assert result["success"] is True
        assert test_file.read_text() == "new content"

class TestShellTool:
    def test_simple_command(self):
        tool = ShellTool(timeout=10)
        result = tool.execute({"command": "echo hello"})
        assert result["success"] is True
        assert "hello" in result["output"]

    def test_command_timeout(self):
        tool = ShellTool(timeout=1)
        result = tool.execute({"command": "sleep 10"})
        assert result["success"] is False

    def test_forbidden_command(self):
        """验证危险命令被拦截（需要权限系统配合）"""
        tool = ShellTool(timeout=10)
        result = tool.execute({"command": "rm -rf /"})
        # 预期：权限系统拦截后返回失败
        # assert result["success"] is False
```

```python
# tests/unit/test_llm.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.llm import LLMClient

class TestLLMClient:
    @pytest.mark.asyncio
    async def test_retry_on_rate_limit(self):
        client = LLMClient(api_key="test", base_url="http://localhost")
        with patch.object(client, "_raw_call") as mock_call:
            from openai import RateLimitError
            mock_call.side_effect = [
                RateLimitError("rate limited", response=MagicMock(), body=None),
                MagicMock(choices=[MagicMock(message=MagicMock(content="ok"))])
            ]
            result = await client.chat([{"role": "user", "content": "hi"}])
            assert mock_call.call_count == 2

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self):
        client = LLMClient(api_key="test", base_url="http://localhost")
        with patch.object(client, "_raw_call") as mock_call:
            from openai import RateLimitError
            mock_call.side_effect = RateLimitError("rate limited", response=MagicMock(), body=None)
            with pytest.raises(RateLimitError):
                await client.chat([{"role": "user", "content": "hi"}])
```

**依赖安装：**

```bash
pip install pytest pytest-asyncio pytest-cov
```

**运行命令：**

```bash
pytest tests/ -v --cov=src --cov-report=html
```

---

### 2. 权限系统

**问题：** Shell 工具可执行任意命令，文件工具可操作任意路径，无安全边界。

**参考：** OpenHarness 实现了多级权限模式 + 路径规则 + 命令黑名单 + 交互式审批。

**建议新增 `src/permissions/` 模块：**

```
src/permissions/
├── __init__.py
├── checker.py          # 权限检查器
├── modes.py            # 权限模式定义
└── rules.py            # 路径与命令规则
```

**核心代码：**

```python
# src/permissions/modes.py
from enum import Enum

class PermissionMode(Enum):
    DEFAULT = "default"      # 写操作/执行前需确认
    AUTO = "auto"            # 允许一切（沙箱/容器环境）
    PLAN = "plan"            # 禁止所有写操作（只读模式）
```

```python
# src/permissions/rules.py
from dataclasses import dataclass, field
from pathlib import Path
import re

@dataclass
class PathRule:
    pattern: str                # glob 模式，如 "/etc/*"
    allow: bool                 # True=允许, False=拒绝
    _compiled: re.Pattern = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self._compiled = re.compile(self._glob_to_regex(self.pattern))

    def matches(self, path: str) -> bool:
        return bool(self._compiled.match(str(Path(path).resolve())))

    @staticmethod
    def _glob_to_regex(pattern: str) -> str:
        """简单 glob → regex 转换"""
        result = pattern.replace(".", r"\.")
        result = result.replace("*", ".*")
        result = result.replace("?", ".")
        return f"^{result}$"

@dataclass
class PermissionConfig:
    mode: PermissionMode = PermissionMode.DEFAULT
    path_rules: list[PathRule] = field(default_factory=list)
    denied_commands: list[str] = field(default_factory=lambda: [
        "rm -rf /",
        "rm -rf /*",
        "mkfs",
        "dd if=",
        ":(){ :|:& };:",
        "shutdown",
        "reboot",
        "format",
    ])
    # 写操作工具列表（DEFAULT 模式下需要确认）
    write_tools: list[str] = field(default_factory=lambda: [
        "file_operation",   # write/append/delete 操作
        "shell",            # 可能修改文件系统
    ])
    # 涉及路径的工具参数名
    path_params: dict[str, str] = field(default_factory=lambda: {
        "file_operation": "path",
        "shell": "command",
    })
```

```python
# src/permissions/checker.py
import logging
from .modes import PermissionMode
from .rules import PermissionConfig, PathRule

logger = logging.getLogger(__name__)

class PermissionCheckResult:
    def __init__(self, allowed: bool, reason: str = ""):
        self.allowed = allowed
        self.reason = reason

class PermissionChecker:
    def __init__(self, config: PermissionConfig):
        self.config = config

    def check(self, tool_name: str, arguments: dict) -> PermissionCheckResult:
        """检查工具调用是否被允许"""

        # AUTO 模式：全部放行
        if self.config.mode == PermissionMode.AUTO:
            return PermissionCheckResult(allowed=True)

        # PLAN 模式：禁止所有写操作
        if self.config.mode == PermissionMode.PLAN:
            if tool_name in self.config.write_tools:
                return PermissionCheckResult(
                    allowed=False,
                    reason=f"PLAN 模式禁止执行写操作工具: {tool_name}"
                )

        # 检查命令黑名单
        if tool_name == "shell":
            command = arguments.get("command", "")
            for denied in self.config.denied_commands:
                if denied in command:
                    return PermissionCheckResult(
                        allowed=False,
                        reason=f"危险命令被拦截: {denied}"
                    )

        # 检查路径规则
        path_param = self.config.path_params.get(tool_name)
        if path_param and path_param in arguments:
            path = arguments[path_param]
            for rule in self.config.path_rules:
                if rule.matches(path) and not rule.allow:
                    return PermissionCheckResult(
                        allowed=False,
                        reason=f"路径被规则拦截: {rule.pattern}"
                    )

        # DEFAULT 模式下写操作需要确认
        if self.config.mode == PermissionMode.DEFAULT:
            if tool_name in self.config.write_tools:
                return PermissionCheckResult(
                    allowed=True,
                    reason="需要用户确认"  # 调用方据此弹出确认
                )

        return PermissionCheckResult(allowed=True)
```

**集成到 Agent：**

```python
# src/agent.py 中修改 _execute_tool 方法
from src.permissions import PermissionChecker, PermissionConfig, PermissionMode

class Agent:
    def __init__(self, ...):
        self.permission = PermissionChecker(PermissionConfig(
            mode=PermissionMode(permission_mode)
        ))

    async def _execute_tool(self, tool_call):
        tool_name = tool_call.function.name
        arguments = json.loads(tool_call.function.arguments)

        # 权限检查
        result = self.permission.check(tool_name, arguments)
        if not result.allowed:
            logger.warning(f"工具调用被拦截: {tool_name}, 原因: {result.reason}")
            return {"role": "tool", "content": f"错误: {result.reason}"}

        # DEFAULT 模式下需要确认（交互模式）
        if result.reason == "需要用户确认":
            confirmed = await self._ask_user_confirmation(tool_name, arguments)
            if not confirmed:
                return {"role": "tool", "content": "用户拒绝执行此操作"}

        # 执行工具
        return await self.tool_registry.execute(tool_call)
```

---

## P1 - 重要

### 3. Pydantic 工具验证

**问题：** 工具参数使用手动 JSON Schema 定义，LLM 可能传入错误类型的参数。

**参考：** OpenHarness 每个工具都有 Pydantic input model，自动生成 JSON Schema 并在运行时验证。

**改造步骤：**

**3a. 定义基类改进：**

```python
# src/tools/base.py（改造现有工具基类）
from pydantic import BaseModel
import json

class BuiltinTool:
    name: str = ""
    description: str = ""
    # 新增：Pydantic 模型（可选，兼容旧工具）
    input_model: type[BaseModel] | None = None
    # 旧方式保留兼容
    parameters: dict = {}

    def get_parameters_schema(self) -> dict:
        """自动从 Pydantic model 生成 JSON Schema"""
        if self.input_model:
            return self.input_model.model_json_schema()
        return self.parameters

    def validate_arguments(self, arguments: dict) -> dict:
        """验证并转换参数"""
        if self.input_model:
            model = self.input_model.model_validate(arguments)
            return model.model_dump()
        return arguments

    async def execute(self, arguments: dict) -> dict:
        raise NotImplementedError
```

**3b. 改造现有工具示例：**

```python
# src/tools/file.py
from pydantic import BaseModel, Field
from typing import Literal, Optional
from src.tools.base import BuiltinTool

class FileOperationInput(BaseModel):
    operation: Literal["read", "write", "append", "delete", "exists", "list"] = Field(
        description="文件操作类型"
    )
    path: str = Field(
        description="目标文件或目录路径"
    )
    content: Optional[str] = Field(
        default=None,
        description="写入/追加的内容（write/append 操作时必需）"
    )
    encoding: str = Field(
        default="utf-8",
        description="文件编码"
    )

    # 跨字段验证
    def model_post_init(self, __context):
        if self.operation in ("write", "append") and self.content is None:
            raise ValueError(f"{self.operation} 操作需要提供 content 参数")

class FileTool(BuiltinTool):
    name = "file_operation"
    description = "文件操作工具：读取、写入、追加、删除、检查存在、列出目录"
    input_model = FileOperationInput

    async def execute(self, arguments: dict) -> dict:
        args = FileOperationInput.model_validate(arguments)
        # 使用 args.operation, args.path, args.content, args.encoding
        # 原有逻辑不变，只是参数现在有类型保障
        ...
```

```python
# src/tools/shell.py
from pydantic import BaseModel, Field
from src.tools.base import BuiltinTool

class ShellInput(BaseModel):
    command: str = Field(description="要执行的 shell 命令")
    timeout: int = Field(default=30, ge=1, le=600, description="超时时间（秒）")
    workdir: str | None = Field(default=None, description="工作目录")

class ShellTool(BuiltinTool):
    name = "shell"
    description = "执行 shell 命令并返回输出"
    input_model = ShellInput
```

**3c. 工具注册表自动处理：**

```python
# src/tools/__init__.py 中修改 ToolRegistry
class ToolRegistry:
    def get_tool_definitions(self) -> list[dict]:
        """生成 OpenAI function calling 格式的工具定义"""
        definitions = []
        for tool in self._tools.values():
            definitions.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.get_parameters_schema(),  # 自动生成
                }
            })
        return definitions

    async def execute(self, tool_call) -> dict:
        tool = self._tools.get(tool_call.function.name)
        arguments = json.loads(tool_call.function.arguments)

        # Pydantic 验证 + 转换
        try:
            arguments = tool.validate_arguments(arguments)
        except Exception as e:
            return {"role": "tool", "content": f"参数验证失败: {e}"}

        return await tool.execute(arguments)
```

---

### 4. 上下文压缩

**问题：** 对话无限增长，最终超出上下文窗口限制。OpenHarness 的 Auto-Compact 能让 agent 运行多日而不需要手动清理。

**建议在 `src/agent_session.py` 中增加：**

```python
# src/agent_session.py 新增方法
import tiktoken

class AgentSession:
    MAX_CONTEXT_TOKENS = 100000  # 根据模型调整

    def count_tokens(self, messages: list[dict]) -> int:
        """计算消息列表的 token 数"""
        try:
            encoding = tiktoken.encoding_for_model(self.model)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")

        total = 0
        for msg in messages:
            total += len(encoding.encode(msg.get("content", "")))
            # 工具调用也计算
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    total += len(encoding.encode(tc.function.arguments))
        return total

    async def compress_if_needed(self, messages: list[dict], llm_client) -> list[dict]:
        """如果上下文过大，压缩历史消息"""
        token_count = self.count_tokens(messages)

        if token_count < self.MAX_CONTEXT_TOKENS * 0.8:
            return messages

        logger.info(f"上下文 token 数 {token_count} 接近上限，开始压缩...")

        # 保留系统消息 + 最近 N 条消息
        system_msgs = [m for m in messages if m["role"] == "system"]
        recent_msgs = messages[-6:]  # 保留最近 3 轮对话
        history_msgs = [m for m in messages
                       if m not in system_msgs and m not in recent_msgs]

        if not history_msgs:
            return messages

        # 用 LLM 压缩历史
        summary = await llm_client.chat([
            {"role": "system", "content": "你是一个对话压缩助手。请将以下对话历史压缩为简洁摘要，保留关键决策、结论和未完成任务。"},
            {"role": "user", "content": f"请压缩以下对话：\n{self._serialize_messages(history_msgs)}"}
        ])

        # 替换历史为摘要
        compressed = [
            *system_msgs,
            {"role": "assistant", "content": f"[对话历史摘要]\n{summary}"},
            *recent_msgs,
        ]

        logger.info(f"上下文压缩完成: {token_count} → {self.count_tokens(compressed)} tokens")
        return compressed
```

**在 Agent 主循环中调用：**

```python
# src/agent.py 的 _think 方法中
async def _think(self, session):
    # 压缩检查
    session.messages = await session.compress_if_needed(
        session.messages, self.llm_client
    )

    response = await self.llm_client.chat(
        messages=session.messages,
        tools=self.tool_registry.get_tool_definitions(),
    )
    ...
```

**依赖：**

```bash
pip install tiktoken
```

---

### 5. 并行工具执行

**问题：** 多个 tool_calls 按顺序执行，即使它们互不依赖。

**参考：** OpenHarness 并行执行独立的工具调用，提升性能。

**修改 `src/agent.py`：**

```python
# 当前（顺序执行）：
async def _execute_tool_calls(self, tool_calls):
    results = []
    for tool_call in tool_calls:
        result = await self._execute_tool(tool_call)
        results.append(result)
    return results

# 改为（并行执行）：
async def _execute_tool_calls(self, tool_calls):
    """并行执行所有工具调用"""
    if len(tool_calls) <= 1:
        # 单个工具调用，无需并行
        return [await self._execute_tool(tc) for tc in tool_calls]

    tasks = [self._execute_tool(tc) for tc in tool_calls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    tool_results = []
    for tc, result in zip(tool_calls, results):
        if isinstance(result, Exception):
            logger.error(f"工具 {tc.function.name} 执行异常: {result}")
            tool_results.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": f"工具执行异常: {type(result).__name__}: {result}"
            })
        else:
            tool_results.append(result)

    return tool_results
```

---

## P2 - 改进

### 6. 钩子系统 (Hooks)

**问题：** 工具执行前后没有拦截点，无法做审计、日志增强、安全检查。

**参考：** OpenHarness 的 PreToolUse / PostToolUse 事件钩子。

**建议新增 `src/hooks/` 模块：**

```
src/hooks/
├── __init__.py
├── manager.py      # 钩子管理器
└── types.py        # 事件类型定义
```

```python
# src/hooks/types.py
from enum import Enum
from dataclasses import dataclass
from typing import Any

class HookEvent(Enum):
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    AGENT_START = "agent_start"
    AGENT_STOP = "agent_stop"
    SESSION_CREATE = "session_create"
    SESSION_CLOSE = "session_close"

@dataclass
class HookContext:
    event: HookEvent
    tool_name: str | None = None
    arguments: dict | None = None
    result: Any | None = None
    error: Exception | None = None
    metadata: dict | None = None

HookCallback = callable  # async (context: HookContext) -> None
```

```python
# src/hooks/manager.py
import logging
from collections import defaultdict
from .types import HookEvent, HookContext, HookCallback

logger = logging.getLogger(__name__)

class HookManager:
    def __init__(self):
        self._hooks: dict[HookEvent, list[HookCallback]] = defaultdict(list)

    def register(self, event: HookEvent, callback: HookCallback):
        self._hooks[event].append(callback)

    async def fire(self, event: HookEvent, **kwargs):
        """触发事件，按顺序执行所有回调"""
        context = HookContext(event=event, **kwargs)
        for callback in self._hooks.get(event, []):
            try:
                await callback(context)
            except Exception as e:
                logger.error(f"钩子回调执行失败 [{event.value}]: {e}")

    def unregister(self, event: HookEvent, callback: HookCallback):
        self._hooks[event].discard(callback)
```

**集成示例 — 审计日志钩子：**

```python
# 在 Agent 初始化时注册
agent.hooks.register(HookEvent.PRE_TOOL_USE, audit_log_hook)
agent.hooks.register(HookEvent.POST_TOOL_USE, audit_log_hook)

async def audit_log_hook(ctx: HookContext):
    if ctx.event == HookEvent.PRE_TOOL_USE:
        logger.info(f"[审计] 开始执行工具: {ctx.tool_name}, 参数: {ctx.arguments}")
    elif ctx.event == HookEvent.POST_TOOL_USE:
        logger.info(f"[审计] 工具执行完成: {ctx.tool_name}")
```

---

### 7. 多 Provider 支持

**问题：** 仅支持 OpenAI 兼容 API。

**参考：** OpenHarness 支持多种后端作为可插拔 profile。

**建议重构 `src/llm.py`：**

```
src/llm/
├── __init__.py
├── base.py          # 抽象基类
├── openai.py        # OpenAI 兼容 Provider
├── anthropic.py     # Anthropic Provider（可选）
└── client.py        # 统一客户端
```

```python
# src/llm/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list | None
    stop_reason: str
    usage: dict  # {"prompt_tokens": int, "completion_tokens": int}

class LLMProvider(ABC):
    @abstractmethod
    async def chat(self, messages: list[dict], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        ...

    @abstractmethod
    async def stream(self, messages: list[dict], **kwargs):
        ...
```

```python
# src/llm/client.py
from .base import LLMProvider, LLMResponse
from .openai import OpenAIProvider

class LLMClient:
    def __init__(self, provider: LLMProvider):
        self._provider = provider
        self._providers: dict[str, LLMProvider] = {}
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def register_provider(self, name: str, provider: LLMProvider):
        self._providers[name] = provider

    def switch_provider(self, name: str):
        if name not in self._providers:
            raise ValueError(f"未注册的 provider: {name}")
        self._provider = self._providers[name]

    async def chat(self, messages: list[dict], **kwargs) -> LLMResponse:
        response = await self._provider.chat(messages, **kwargs)
        # 记录用量
        if response.usage:
            self.total_prompt_tokens += response.usage.get("prompt_tokens", 0)
            self.total_completion_tokens += response.usage.get("completion_tokens", 0)
        return response

    def get_usage_summary(self) -> dict:
        return {
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
        }
```

---

### 8. Token 与成本追踪

**问题：** 没有记录 LLM 调用的 token 用量和成本。

**建议新增 `src/usage.py`：**

```python
# src/usage.py
import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# 模型定价（每百万 token，单位：元）
MODEL_PRICING = {
    "gpt-4o": {"input": 17.5, "output": 70.0},
    "gpt-4o-mini": {"input": 1.05, "output": 4.2},
    "gpt-4-turbo": {"input": 70.0, "output": 210.0},
    "deepseek-chat": {"input": 1.0, "output": 2.0},
    "deepseek-reasoner": {"input": 4.0, "output": 16.0},
    "qwen-max": {"input": 20.0, "output": 60.0},
    "qwen-plus": {"input": 4.0, "output": 12.0},
    # 添加更多模型...
}

@dataclass
class UsageRecord:
    timestamp: datetime
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost: float

@dataclass
class UsageTracker:
    records: list[UsageRecord] = field(default_factory=list)

    def track(self, model: str, usage: dict):
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

        pricing = MODEL_PRICING.get(model, {"input": 0, "output": 0})
        cost = (prompt_tokens * pricing["input"] + completion_tokens * pricing["output"]) / 1_000_000

        record = UsageRecord(
            timestamp=datetime.now(),
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
        )
        self.records.append(record)
        logger.info(f"[用量] {model}: {prompt_tokens}+{completion_tokens} tokens, ¥{cost:.4f}")

    def get_summary(self) -> dict:
        total_prompt = sum(r.prompt_tokens for r in self.records)
        total_completion = sum(r.completion_tokens for r in self.records)
        total_cost = sum(r.cost for r in self.records)
        return {
            "total_calls": len(self.records),
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "total_cost_cny": round(total_cost, 4),
        }
```

**集成到 LLMClient：**

```python
# src/llm.py 或 src/llm/client.py 中
from src.usage import UsageTracker

class LLMClient:
    def __init__(self, ...):
        self.usage_tracker = UsageTracker()

    async def chat(self, messages, **kwargs):
        response = await self._call_api(messages, **kwargs)
        self.usage_tracker.track(self.model, response.usage)
        return response
```

**在交互模式中查看：**

```python
# src/cmd_handler.py 新增命令
elif cmd == "/usage":
    summary = self.agent.llm_client.usage_tracker.get_summary()
    console.print(f"""
    [bold]LLM 用量统计[/bold]
    ──────────────────
    调用次数: {summary['total_calls']}
    输入 Token: {summary['total_prompt_tokens']:,}
    输出 Token: {summary['total_completion_tokens']:,}
    总 Token: {summary['total_tokens']:,}
    总费用: ¥{summary['total_cost_cny']}
    """)
```

---

## P3 - 增强

### 9. 非 CLI 输出模式

**问题：** 只有交互式 CLI，没有 programmatic 接口。

**参考：** OpenHarness 支持 `-p` 单次执行、`--output-format json`、`--output-format stream-json`。

**建议修改 `src/main.py`：**

```python
# src/main.py 新增参数
parser.add_argument("-p", "--print", dest="prompt", help="单次执行模式")
parser.add_argument("--output-format", choices=["text", "json", "stream-json"],
                    default="text", help="输出格式")

# 单次执行模式
if args.prompt:
    result = await agent.run(args.prompt)
    if args.output_format == "json":
        print(json.dumps({"response": result}, ensure_ascii=False, indent=2))
    elif args.output_format == "stream-json":
        # 流式输出 JSON 事件
        async for event in agent.run_stream(args.prompt):
            print(json.dumps(event, ensure_ascii=False))
    else:
        print(result)
    return
```

**使用示例：**

```bash
# 单次执行
python src/main.py -p "检查设备SN123状态"

# JSON 输出（便于脚本集成）
python src/main.py -p "检查设备SN123状态" --output-format json

# 流式 JSON（便于其他系统集成）
python src/main.py -p "检查设备SN123状态" --output-format stream-json
```

---

### 10. 插件系统增强

**问题：** 当前插件只支持 start/stop，功能单一。

**参考：** OpenHarness 插件可以注册命令、钩子、工具、代理模板。

**建议增强 `src/plugins/base.py`：**

```python
# src/plugins/base.py
from abc import ABC, abstractmethod

class BasePlugin(ABC):
    @abstractmethod
    async def start(self): ...

    @abstractmethod
    async def stop(self): ...

    def _load_config(self): ...

    # 新增：可覆写的扩展点
    def register_commands(self) -> list[dict]:
        """注册 CLI 命令。返回 [{"name": "/xxx", "handler": fn, "help": "..."}]"""
        return []

    def register_hooks(self) -> list[dict]:
        """注册生命周期钩子。返回 [{"event": "pre_tool_use", "callback": fn}]"""
        return []

    def register_tools(self) -> list:
        """注册自定义工具。返回 [BuiltinTool 实例]"""
        return []

    def register_agents(self) -> list[dict]:
        """注册子代理模板。返回 [{"name": "...", "prompt": "..."}]"""
        return []
```

**插件管理器修改：**

```python
# src/plugins/manager.py
class PluginManager:
    async def load_plugin(self, plugin: BasePlugin):
        await plugin.start()

        # 注册扩展
        for cmd in plugin.register_commands():
            self._command_registry[cmd["name"]] = cmd
        for hook in plugin.register_hooks():
            self._hook_manager.register(hook["event"], hook["callback"])
        for tool in plugin.register_tools():
            self._tool_registry.register(tool)
        for agent in plugin.register_agents():
            self._agent_templates[agent["name"]] = agent
```

---

### 11. CI/CD 配置

**问题：** 无 CI/CD，无自动化质量保障。

**建议添加 `.github/workflows/ci.yml`：**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.10'
      - run: pip install ruff
      - run: ruff check src/ tests/

  test:
    runs-on: ubuntu-latest
    needs: lint
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.10'
      - run: pip install -r requirements.txt
      - run: pip install pytest pytest-asyncio pytest-cov
      - run: pytest tests/ -v --cov=src --cov-report=xml
      - uses: codecov/codecov-action@v4
        with:
          files: coverage.xml

  docker:
    runs-on: ubuntu-latest
    needs: test
    steps:
      - uses: actions/checkout@v4
      - run: docker build -t agent .
      - run: docker run --rm agent python -c "from src.agent import Agent; print('OK')"
```

**同时建议添加 `pyproject.toml` 中的 ruff 配置：**

```toml
[tool.ruff]
target-version = "py310"
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "W", "I", "N", "UP", "B", "SIM"]
ignore = ["E501"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

---

### 12. MCP HTTP 传输

**问题：** MCP 仅支持 stdio 传输，无法连接远程 MCP 服务器。

**参考：** OpenHarness 支持 HTTP 传输 + 自动重连 + 断线优雅降级。

**建议修改 `src/mcps/manager.py`：**

```python
# src/mcps/manager.py 新增 HTTP 传输支持
from pymcp.client transports import StdioTransport, HTTPTransport

def _create_transport(self, config: dict):
    transport_type = config.get("transport", "stdio")

    if transport_type == "stdio":
        return StdioTransport(
            command=config["command"],
            args=config.get("args", []),
            env=config.get("env"),
        )
    elif transport_type == "http":
        return HTTPTransport(
            url=config["url"],
            headers=config.get("headers", {}),
            timeout=config.get("timeout", 30),
        )
    else:
        raise ValueError(f"不支持的传输类型: {transport_type}")
```

**配置示例（`workspace/mcp_servers.json`）：**

```json
{
  "mcp_servers": {
    "local-device": {
      "transport": "stdio",
      "command": "python",
      "args": ["mcp_server/src/device_ops.py"],
      "enabled": true
    },
    "remote-api": {
      "transport": "http",
      "url": "https://internal-api.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${API_TOKEN}"
      },
      "timeout": 30,
      "enabled": true
    }
  }
}
```

---

## P0 - 工具能力补强

### 13. 工具能力全景对比与缺口分析

**当前工具 vs OpenHarness（43+ 工具）对比：**

| 类别 | OpenHarness | 本项目 | 缺口 |
|------|-------------|--------|------|
| **文件 I/O** | Read, Write, Edit, Glob, Grep, Bash | file_operation (read/write/append/delete/exists/list) | 缺 Edit（行级编辑）、Glob（文件搜索）、Grep（内容搜索） |
| **Web** | WebFetch, WebSearch | 无 | 完全缺失 |
| **搜索** | ToolSearch, LSP | 无 | 完全缺失 |
| **Agent** | Agent, SendMessage, TeamCreate/Delete | subagent（仅创建） | 缺消息传递、团队协作 |
| **后台任务** | TaskCreate/Get/List/Update/Stop/Output | 无 | 完全缺失 |
| **调度** | CronCreate/List/Delete, RemoteTrigger | 无（scheduler 是外部的） | 完全缺失 |
| **交互** | AskUser | 无 | 完全缺失 |
| **技能** | Skill（按需加载） | execute_skill | 可用但较弱 |
| **模式** | EnterPlanMode, ExitPlanMode | 无 | 完全缺失 |
| **笔记本** | NotebookEdit | 无 | 完全缺失 |

### 14. 新增文件搜索工具 — GrepTool

**问题：** Agent 无法在代码库中搜索内容，这是最基本的能力缺失。当前只能用 `shell` 执行 `grep` 命令，效率低且输出不结构化。

**建议新增 `src/tools/grep.py`：**

```python
import os
import re
import json
from typing import Dict, Any, Optional
from . import BuiltinTool


class GrepTool(BuiltinTool):
    """文件内容搜索工具 — 在指定目录中递归搜索匹配正则表达式的内容"""

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return """在文件中搜索匹配指定模式的内容。
支持正则表达式搜索，可按文件类型过滤，返回匹配的文件路径、行号和匹配内容。

使用场景：
- 在代码库中查找特定函数、类、变量的定义或引用
- 搜索配置文件中的特定设置
- 查找包含特定关键词的所有文件
- 按文件类型（如 .py, .json, .yaml）搜索"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "搜索的正则表达式模式"
                },
                "path": {
                    "type": "string",
                    "description": "搜索的根目录路径，默认当前工作目录"
                },
                "file_pattern": {
                    "type": "string",
                    "description": "文件名过滤模式（glob格式），如 '*.py', '*.{json,yaml}'",
                    "default": "*"
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "是否忽略大小写",
                    "default": False
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回结果数",
                    "default": 50
                },
                "context_lines": {
                    "type": "integer",
                    "description": "显示匹配行前后的上下文行数",
                    "default": 2
                }
            },
            "required": ["pattern"]
        }

    async def execute(self, **kwargs) -> str:
        pattern = kwargs.get("pattern", "")
        search_path = kwargs.get("path", os.getcwd())
        file_pattern = kwargs.get("file_pattern", "*")
        case_insensitive = kwargs.get("case_insensitive", False)
        max_results = kwargs.get("max_results", 50)
        context_lines = kwargs.get("context_lines", 2)

        if not pattern:
            return json.dumps({"success": False, "error": "搜索模式不能为空"}, ensure_ascii=False)

        if not os.path.isdir(search_path):
            return json.dumps({"success": False, "error": f"目录不存在: {search_path}"}, ensure_ascii=False)

        try:
            flags = re.IGNORECASE if case_insensitive else 0
            regex = re.compile(pattern, flags)
        except re.error as e:
            return json.dumps({"success": False, "error": f"正则表达式错误: {e}"}, ensure_ascii=False)

        import fnmatch
        matches = []
        files_searched = 0

        for root, dirs, files in os.walk(search_path):
            # 跳过隐藏目录和常见忽略目录
            dirs[:] = [d for d in dirs if not d.startswith('.')
                       and d not in ('__pycache__', 'node_modules', '.git', '.venv', 'venv')]

            for filename in files:
                if not fnmatch.fnmatch(filename, file_pattern):
                    continue

                filepath = os.path.join(root, filename)
                files_searched += 1

                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                except (OSError, PermissionError):
                    continue

                for i, line in enumerate(lines):
                    if regex.search(line):
                        # 获取上下文行
                        start = max(0, i - context_lines)
                        end = min(len(lines), i + context_lines + 1)
                        context = "".join(lines[start:end])

                        matches.append({
                            "file": filepath,
                            "line": i + 1,
                            "content": line.rstrip(),
                            "context": context.rstrip()
                        })

                        if len(matches) >= max_results:
                            return json.dumps({
                                "success": True,
                                "pattern": pattern,
                                "files_searched": files_searched,
                                "total_matches": len(matches),
                                "truncated": True,
                                "matches": matches
                            }, ensure_ascii=False)

        return json.dumps({
            "success": True,
            "pattern": pattern,
            "files_searched": files_searched,
            "total_matches": len(matches),
            "truncated": False,
            "matches": matches
        }, ensure_ascii=False)
```

### 15. 新增文件查找工具 — GlobTool

**问题：** Agent 无法按文件名模式搜索文件，只能用 `file_operation list` 列出单个目录。

**建议新增 `src/tools/glob.py`：**

```python
import os
import json
import fnmatch
from typing import Dict, Any
from . import BuiltinTool


class GlobTool(BuiltinTool):
    """文件名模式搜索工具 — 按 glob 模式查找文件"""

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return """按文件名模式快速查找文件。
支持 glob 通配符：* 匹配任意字符，? 匹配单个字符，** 匹配多级目录。

使用场景：
- 查找项目中所有 Python 文件：'**/*.py'
- 查找特定名称的配置文件：'**/config*.json'
- 查找测试文件：'**/test_*.py'
- 查找某个目录下的所有文件：'src/**/*'"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "glob 匹配模式，如 '**/*.py', 'src/**/*.json', '*.md'"
                },
                "path": {
                    "type": "string",
                    "description": "搜索的根目录，默认当前工作目录"
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回文件数",
                    "default": 100
                }
            },
            "required": ["pattern"]
        }

    async def execute(self, **kwargs) -> str:
        pattern = kwargs.get("pattern", "")
        search_path = kwargs.get("path", os.getcwd())
        max_results = kwargs.get("max_results", 100)

        if not pattern:
            return json.dumps({"success": False, "error": "模式不能为空"}, ensure_ascii=False)

        matches = []
        # 处理 ** 模式
        if "**" in pattern:
            for root, dirs, files in os.walk(search_path):
                dirs[:] = [d for d in dirs if not d.startswith('.')
                           and d not in ('__pycache__', 'node_modules', '.git', '.venv', 'venv')]
                for filename in files:
                    # 将 pattern 中的 ** 替换为实际路径匹配
                    rel_pattern = pattern.replace("**/", "")
                    if fnmatch.fnmatch(filename, rel_pattern):
                        matches.append(os.path.join(root, filename))
                        if len(matches) >= max_results:
                            break
                if len(matches) >= max_results:
                    break
        else:
            # 简单模式，只匹配当前目录
            for filename in os.listdir(search_path):
                if fnmatch.fnmatch(filename, pattern):
                    full_path = os.path.join(search_path, filename)
                    matches.append(full_path)
                    if len(matches) >= max_results:
                        break

        return json.dumps({
            "success": True,
            "pattern": pattern,
            "path": search_path,
            "count": len(matches),
            "files": sorted(matches)
        }, ensure_ascii=False)
```

### 16. 新增行级文件编辑工具 — EditTool

**问题：** `file_operation` 的 write 操作只能全量覆写文件。对大文件做小改动时，Agent 需要先读取全文、再写回全文，效率低且容易出错。

**建议新增 `src/tools/edit.py`：**

```python
import os
import json
from typing import Dict, Any
from . import BuiltinTool


class EditTool(BuiltinTool):
    """行级文件编辑工具 — 精确替换文件中的指定内容"""

    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return """对文件进行精确的行级编辑。通过指定旧内容和新内容来替换文件中的特定部分。
支持在同一文件上执行多次替换操作。

使用场景：
- 修改代码中的某个函数实现
- 更新配置文件中的某个值
- 修复文件中的特定错误
- 重命名变量或函数

注意：old_text 必须是文件中唯一存在的文本，否则操作会失败。"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要编辑的文件路径"
                },
                "old_text": {
                    "type": "string",
                    "description": "要被替换的原文本（必须精确匹配）"
                },
                "new_text": {
                    "type": "string",
                    "description": "替换后的新文本"
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换所有匹配项（默认仅替换第一个匹配）",
                    "default": False
                }
            },
            "required": ["path", "old_text", "new_text"]
        }

    async def execute(self, **kwargs) -> str:
        path = kwargs.get("path", "")
        old_text = kwargs.get("old_text", "")
        new_text = kwargs.get("new_text", "")
        replace_all = kwargs.get("replace_all", False)

        if not path:
            return json.dumps({"success": False, "error": "文件路径不能为空"}, ensure_ascii=False)
        if not old_text:
            return json.dumps({"success": False, "error": "原文本不能为空"}, ensure_ascii=False)
        if old_text == new_text:
            return json.dumps({"success": False, "error": "原文本和新文本相同"}, ensure_ascii=False)

        if not os.path.exists(path):
            return json.dumps({"success": False, "error": f"文件不存在: {path}"}, ensure_ascii=False)
        if os.path.isdir(path):
            return json.dumps({"success": False, "error": f"路径是目录: {path}"}, ensure_ascii=False)

        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return json.dumps({"success": False, "error": f"读取文件失败: {e}"}, ensure_ascii=False)

        # 检查是否唯一
        occurrences = content.count(old_text)
        if occurrences == 0:
            return json.dumps({"success": False, "error": "未找到匹配的文本"}, ensure_ascii=False)
        if occurrences > 1 and not replace_all:
            return json.dumps({
                "success": False,
                "error": f"找到 {occurrences} 处匹配，请提供更多上下文使匹配唯一，或设置 replace_all=true"
            }, ensure_ascii=False)

        # 执行替换
        if replace_all:
            new_content = content.replace(old_text, new_text)
        else:
            new_content = content.replace(old_text, new_text, 1)

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            return json.dumps({"success": False, "error": f"写入文件失败: {e}"}, ensure_ascii=False)

        return json.dumps({
            "success": True,
            "path": path,
            "replacements": occurrences if replace_all else 1,
            "message": f"成功替换 {occurrences if replace_all else 1} 处匹配"
        }, ensure_ascii=False)
```

### 17. 新增 Web 搜索工具 — WebSearchTool

**问题：** Agent 完全没有 Web 能力，无法搜索互联网信息或获取网页内容。对于企业内部 Agent，至少需要访问内部知识库和文档。

**建议新增 `src/tools/web.py`：**

```python
import json
import logging
from typing import Dict, Any
import httpx
from . import BuiltinTool

logger = logging.getLogger("agent.tools")


class WebSearchTool(BuiltinTool):
    """网络搜索工具 — 搜索互联网获取最新信息"""

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return """搜索互联网获取最新信息。
返回搜索结果列表，包含标题、摘要和链接。

使用场景：
- 查询技术文档和最新API信息
- 搜索解决方案和最佳实践
- 获取最新的新闻和动态
- 查找开源项目和库的使用方法"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词"
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回结果数",
                    "default": 10
                }
            },
            "required": ["query"]
        }

    async def execute(self, **kwargs) -> str:
        query = kwargs.get("query", "")
        max_results = kwargs.get("max_results", 10)

        if not query:
            return json.dumps({"success": False, "error": "搜索关键词不能为空"}, ensure_ascii=False)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://api.duckduckgo.com/",
                    params={
                        "q": query,
                        "format": "json",
                        "no_html": 1,
                        "skip_disambig": 1,
                    }
                )
                data = resp.json()

                results = []
                # 解析 DuckDuckGo 结果
                abstract = data.get("Abstract")
                if abstract:
                    results.append({
                        "title": data.get("Heading", ""),
                        "snippet": abstract,
                        "url": data.get("AbstractURL", ""),
                    })

                for topic in (data.get("RelatedTopics") or [])[:max_results]:
                    if isinstance(topic, dict) and "Text" in topic:
                        results.append({
                            "title": topic.get("Text", "")[:80],
                            "snippet": topic.get("Text", ""),
                            "url": topic.get("FirstURL", ""),
                        })

                return json.dumps({
                    "success": True,
                    "query": query,
                    "count": len(results),
                    "results": results[:max_results]
                }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"Web搜索失败: {e}")
            return json.dumps({"success": False, "error": f"搜索失败: {e}"}, ensure_ascii=False)


class WebFetchTool(BuiltinTool):
    """网页内容获取工具 — 获取指定 URL 的网页内容"""

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return """获取指定URL的网页内容，返回纯文本或Markdown格式。
可用于获取文档页面、API说明、网页内容等。

使用场景：
- 获取在线文档内容
- 读取网页中的信息
- 获取API响应数据"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要获取的网页URL"
                },
                "max_length": {
                    "type": "integer",
                    "description": "返回内容的最大字符数",
                    "default": 10000
                }
            },
            "required": ["url"]
        }

    async def execute(self, **kwargs) -> str:
        url = kwargs.get("url", "")
        max_length = kwargs.get("max_length", 10000)

        if not url:
            return json.dumps({"success": False, "error": "URL不能为空"}, ensure_ascii=False)

        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; AgentBot/1.0)"
                })
                resp.raise_for_status()

                content = resp.text
                # 简单的 HTML → 纯文本转换
                if "<html" in content.lower():
                    content = self._html_to_text(content)

                if len(content) > max_length:
                    content = content[:max_length] + "\n... [内容已截断]"

                return json.dumps({
                    "success": True,
                    "url": url,
                    "status_code": resp.status_code,
                    "content_length": len(content),
                    "content": content
                }, ensure_ascii=False)

        except httpx.HTTPError as e:
            return json.dumps({"success": False, "error": f"HTTP请求失败: {e}"}, ensure_ascii=False)

    @staticmethod
    def _html_to_text(html: str) -> str:
        """简单 HTML 转 text（生产环境建议用 beautifulsoup4）"""
        import re
        # 移除 script/style
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
        # 移除 HTML 标签
        html = re.sub(r'<[^>]+>', ' ', html)
        # 压缩空白
        html = re.sub(r'\s+', ' ', html).strip()
        return html
```

### 18. 新增后台任务工具 — TaskTool

**问题：** Agent 无法创建后台任务。长时间运行的操作（如批量设备巡检）会阻塞主对话。

**参考：** OpenHarness 有完整的 TaskCreate/Get/List/Update/Stop/Output 工具组。

**建议新增 `src/tools/task.py`：**

```python
import asyncio
import json
import uuid
from datetime import datetime
from typing import Dict, Any, Optional
from dataclasses import dataclass, field, asdict
from . import BuiltinTool


@dataclass
class BackgroundTask:
    id: str
    description: str
    status: str = "pending"       # pending / running / completed / failed / cancelled
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    result: Optional[str] = None
    error: Optional[str] = None
    _async_task: Optional[asyncio.Task] = field(default=None, repr=False)


class TaskManager:
    """后台任务管理器"""
    def __init__(self):
        self._tasks: Dict[str, BackgroundTask] = {}

    def create_task(self, description: str) -> BackgroundTask:
        task_id = str(uuid.uuid4())[:8]
        task = BackgroundTask(id=task_id, description=description)
        self._tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> Optional[BackgroundTask]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list:
        return [
            {"id": t.id, "description": t.description, "status": t.status,
             "created_at": t.created_at, "error": t.error}
            for t in self._tasks.values()
        ]

    async def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or not task._async_task:
            return False
        task._async_task.cancel()
        task.status = "cancelled"
        return True


class TaskCreateTool(BuiltinTool):
    def __init__(self, task_manager: TaskManager):
        self.task_manager = task_manager

    @property
    def name(self) -> str:
        return "task_create"

    @property
    def description(self) -> str:
        return "创建一个后台任务。任务将在后台异步执行，不阻塞当前对话。"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "任务描述"
                }
            },
            "required": ["description"]
        }

    async def execute(self, **kwargs) -> str:
        description = kwargs.get("description", "")
        task = self.task_manager.create_task(description)
        return json.dumps({
            "success": True, "task_id": task.id,
            "status": task.status, "description": task.description
        }, ensure_ascii=False)


class TaskListTool(BuiltinTool):
    def __init__(self, task_manager: TaskManager):
        self.task_manager = task_manager

    @property
    def name(self) -> str:
        return "task_list"

    @property
    def description(self) -> str:
        return "列出所有后台任务及其状态"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        tasks = self.task_manager.list_tasks()
        return json.dumps({"success": True, "count": len(tasks), "tasks": tasks}, ensure_ascii=False)


class TaskGetTool(BuiltinTool):
    def __init__(self, task_manager: TaskManager):
        self.task_manager = task_manager

    @property
    def name(self) -> str:
        return "task_get"

    @property
    def description(self) -> str:
        return "获取指定后台任务的详细信息和结果"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务ID"}
            },
            "required": ["task_id"]
        }

    async def execute(self, **kwargs) -> str:
        task_id = kwargs.get("task_id", "")
        task = self.task_manager.get_task(task_id)
        if not task:
            return json.dumps({"success": False, "error": f"任务不存在: {task_id}"}, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "task": {
                "id": task.id, "description": task.description,
                "status": task.status, "created_at": task.created_at,
                "result": task.result, "error": task.error
            }
        }, ensure_ascii=False)


class TaskCancelTool(BuiltinTool):
    def __init__(self, task_manager: TaskManager):
        self.task_manager = task_manager

    @property
    def name(self) -> str:
        return "task_cancel"

    @property
    def description(self) -> str:
        return "取消一个正在运行的后台任务"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "要取消的任务ID"}
            },
            "required": ["task_id"]
        }

    async def execute(self, **kwargs) -> str:
        task_id = kwargs.get("task_id", "")
        success = await self.task_manager.cancel_task(task_id)
        return json.dumps({"success": success, "task_id": task_id}, ensure_ascii=False)
```

### 19. 新增用户交互工具 — AskUserTool

**问题：** Agent 执行过程中无法向用户提问或请求确认，只能盲执行。

**建议新增 `src/tools/ask_user.py`：**

```python
import json
from typing import Dict, Any
from . import BuiltinTool


class AskUserTool(BuiltinTool):
    """用户交互工具 — 在执行过程中向用户提问或请求确认"""

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def description(self) -> str:
        return """在执行过程中向用户提问，获取用户的输入或确认。
会暂停 Agent 执行，等待用户回复后继续。

使用场景：
- 执行危险操作前请求确认
- 需要用户提供额外信息
- 提供多个选项让用户选择
- 展示中间结果请用户决策"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要向用户提问的问题"
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选的选项列表，用户可以从中选择"
                },
                "default": {
                    "type": "string",
                    "description": "默认值（用户直接回车时使用）"
                }
            },
            "required": ["question"]
        }

    def __init__(self, input_handler=None):
        self._input_handler = input_handler  # 外部注入的输入处理函数

    async def execute(self, **kwargs) -> str:
        question = kwargs.get("question", "")
        options = kwargs.get("options", [])
        default = kwargs.get("default", "")

        if not question:
            return json.dumps({"success": False, "error": "问题不能为空"}, ensure_ascii=False)

        if self._input_handler:
            answer = await self._input_handler(question, options, default)
        else:
            # 默认控制台交互
            if options:
                print(f"\n{question}")
                for i, opt in enumerate(options, 1):
                    print(f"  {i}. {opt}")
                raw = input(f"请选择 (1-{len(options)}" +
                            (f", 默认: {default}" if default else "") + "): ").strip()
                if raw == "" and default:
                    answer = default
                elif raw.isdigit() and 1 <= int(raw) <= len(options):
                    answer = options[int(raw) - 1]
                else:
                    answer = raw
            else:
                prompt = question + (f" (默认: {default})" if default else "") + ": "
                raw = input(prompt).strip()
                answer = raw if raw else default

        return json.dumps({
            "success": True,
            "question": question,
            "answer": answer
        }, ensure_ascii=False)
```

### 20. 现有工具的能力增强

**20a. FileTool 增强 — 分段读取 + 行号 + 大文件安全**

当前 `file_operation read` 问题：
- 读取整个文件，大文件可能超出上下文
- 没有行号信息，Agent 无法精确定位
- 没有文件大小限制

```python
# 在 FileTool._read_file 中增加：
def _read_file(self, path: str, encoding: str, offset: int = 0, limit: int = None) -> str:
    if not os.path.exists(path):
        return json.dumps({"success": False, "error": f"文件不存在: {path}"}, ensure_ascii=False)

    file_size = os.path.getsize(path)
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB 安全限制

    if file_size > MAX_FILE_SIZE:
        return json.dumps({
            "success": False,
            "error": f"文件过大 ({file_size // 1024 // 1024}MB)，请使用 offset/limit 分段读取"
        }, ensure_ascii=False)

    with open(path, "r", encoding=encoding) as f:
        lines = f.readlines()

    # 分段读取
    end = offset + limit if limit else len(lines)
    selected_lines = lines[offset:end]

    # 带行号输出
    numbered = "\n".join(
        f"{offset + i + 1:6d}\t{line.rstrip()}"
        for i, line in enumerate(selected_lines)
    )

    return json.dumps({
        "success": True,
        "path": path,
        "total_lines": len(lines),
        "showing": f"{offset + 1}-{min(end, len(lines))}",
        "content": numbered
    }, ensure_ascii=False)
```

新增 parameters：
```python
"offset": {
    "type": "integer",
    "description": "起始行号（0-based），默认从文件开头",
    "default": 0
},
"limit": {
    "type": "integer",
    "description": "读取的行数，默认读取全部",
    "default": null
}
```

**20b. ShellTool 增强 — 环境变量注入 + 安全限制**

当前问题：无资源限制，`rm -rf` 等危险命令无拦截。

```python
# ShellTool 增强参数：
"env": {
    "type": "object",
    "description": "环境变量，如 {'DEBUG': '1', 'PATH': '/usr/bin'}"
},
"max_output": {
    "type": "integer",
    "description": "输出最大字符数，默认 10000",
    "default": 10000
}

# 执行时注入环境变量：
env = {**os.environ, **(kwargs.get("env", {}))}
process = await asyncio.create_subprocess_shell(
    command, stdout=..., stderr=...,
    cwd=cwd, env=env
)
```

**20c. TodoTool 增强 — 持久化 + 查询过滤**

当前问题：Todo 数据纯内存，重启丢失；无法按状态查询。

```python
# TodoTool 增加持久化
import json

class TodoTool(BuiltinTool):
    def __init__(self, persist_path: str = None):
        self._todos: Dict[str, TodoItem] = {}
        self._persist_path = persist_path
        if persist_path and os.path.exists(persist_path):
            self._load()

    def _load(self):
        with open(self._persist_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for item in data:
            self._todos[item["id"]] = TodoItem(**item)

    def _save(self):
        if self._persist_path:
            with open(self._persist_path, "w", encoding="utf-8") as f:
                json.dump([asdict(t) for t in self._todos.values()], f, ensure_ascii=False, indent=2)

    # 每次修改后调用 self._save()
```

增加参数支持按状态过滤和更新单个 todo：
```python
# parameters 新增 operation 字段，替代当前的全量覆盖模式
"operation": {
    "type": "string",
    "enum": ["add", "update", "delete", "list", "clear"],
    "description": "操作类型"
},
"filter_status": {
    "type": "string",
    "enum": ["pending", "in_progress", "completed", "cancelled"],
    "description": "按状态过滤（list 操作时使用）"
},
"todo_id": {
    "type": "string",
    "description": "要更新/删除的 todo ID"
}
```

**20d. MemoryTool 增强 — 语义搜索**

当前问题：`search` 操作只是简单关键词匹配（`load_memory(query)`），搜索能力弱。

```python
# 方案 1：利用 LLM 做语义匹配（无需额外依赖）
async def _search(self, args: Dict[str, Any]) -> str:
    query = args.get("query", "")
    all_memories = self.memory_manager.load_memory("")

    if not all_memories:
        return json.dumps({"success": True, "results": []}, ensure_ascii=False)

    # 用 LLM 做语义匹配
    prompt = f"""从以下记忆中找出与查询最相关的内容。
查询: {query}

记忆内容:
{all_memories}

请返回相关的记忆片段，按相关度排序。如果没有相关内容，返回空列表。"""

    result = await self.llm_client.chat([
        {"role": "system", "content": "你是记忆搜索助手。"},
        {"role": "user", "content": prompt}
    ])
    # ...

# 方案 2：引入向量数据库（适合大规模记忆）
# pip install chromadb sentence-transformers
# 将记忆 embedding 后存入 chromadb，搜索时用向量相似度
```

---

## P1 - Agent 核心循环优化

### 21. LLMClient 同步阻塞问题

**问题：** `LLMClient.chat()` 使用同步 `OpenAI` 客户端 + `time.sleep()` 阻塞重试。在 async Agent 循环中调用会阻塞整个事件循环，影响所有并发会话。

**当前代码（`src/llm.py`）：**
```python
# 问题 1：同步客户端
self.client = OpenAI(...)  # 同步

# 问题 2：同步阻塞
response = self.client.chat.completions.create(**params)  # 阻塞

# 问题 3：time.sleep 阻塞事件循环
time.sleep(delay)  # 阻塞所有协程
```

**修复方案：**

```python
from openai import AsyncOpenAI  # 使用异步客户端

class LLMClient:
    def __init__(self, ...):
        self.client = AsyncOpenAI(  # 改为异步
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=60.0
        )

    async def chat(self, messages, tools, stream=False, use_cache=True):
        # ...
        for attempt in range(MAX_RETRIES):
            try:
                response = await self.client.chat.completions.create(**params)  # 异步等待
                return response
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    delay = self._calculate_retry_delay(attempt, e)
                    await asyncio.sleep(delay)  # 异步等待，不阻塞事件循环
                else:
                    raise
```

### 22. Agent 循环中 _think 的同步调用

**问题：** `src/agent.py` 中 `_think` 声明为 async 但 `self.client.chat()` 是同步调用。

```python
# 当前（agent.py:329-334）：
async def _think(self, messages):
    response = self.client.chat(messages, self.tool_defs, stream=False)
    # 虽然在 async 函数中，但 chat() 是同步的，会阻塞
```

**修复：** 配合上面 LLMClient 改为异步后，加 `await`：
```python
async def _think(self, messages):
    response = await self.client.chat(messages, self.tool_defs, stream=False)
```

### 23. Agent 错误恢复

**问题：** `src/agent.py` 中一次工具执行异常就终止整个任务。

```python
# 当前（agent.py:304-308）：
except Exception as e:
    self.status = "failed"
    logger.error(f"Agent failed: {e}")
```

**建议 — 工具级别容错：**

```python
for i in range(self.max_iterations):
    try:
        response = await self._think(session.messages)
        msg = response.get("message", {})

        session.add_message("assistant", msg.get("content") or "",
                          tool_calls=msg.get("tool_calls"))

        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                try:
                    result = await self._execute_tool(func_name, func_args)
                except Exception as e:
                    # 工具执行失败不终止，将错误信息反馈给 LLM
                    logger.error(f"工具 {func_name} 执行失败: {e}")
                    result = json.dumps({
                        "success": False,
                        "error": f"工具执行失败: {type(e).__name__}: {e}"
                    }, ensure_ascii=False)

                session.add_message("tool", str(result), ...)
            continue

        if msg.get("content"):
            self.status = "completed"
            self.result = msg["content"]
            break

    except Exception as e:
        # LLM 调用失败，尝试继续
        logger.error(f"第 {i+1} 轮思考失败: {e}")
        session.add_message("system", f"上一轮思考出错: {e}，请重试")

        # 连续 3 次 LLM 错误才放弃
        if not hasattr(self, '_consecutive_errors'):
            self._consecutive_errors = 0
        self._consecutive_errors += 1
        if self._consecutive_errors >= 3:
            self.status = "failed"
            break
```

### 24. 流式响应支持

**问题：** LLM 响应全部完成后才返回，用户等待时间长。没有打字机效果。

**建议在 Agent 循环中增加流式回调：**

```python
class Agent:
    def __init__(self, ...):
        self.on_token = None  # 外部注册的流式回调

    async def _think(self, messages):
        if self.on_token:
            # 流式模式
            stream = await self.client.chat(
                messages, self.tool_defs, stream=True
            )
            content = ""
            tool_calls_accumulator = {}

            async for chunk in stream:
                delta = chunk.choices[0].delta

                if delta.content:
                    content += delta.content
                    await self.on_token(delta.content)  # 实时推送

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_accumulator:
                            tool_calls_accumulator[idx] = {
                                "id": tc.id or "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""}
                            }
                        if tc.function.name:
                            tool_calls_accumulator[idx]["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            tool_calls_accumulator[idx]["function"]["arguments"] += tc.function.arguments

            tool_calls = list(tool_calls_accumulator.values()) if tool_calls_accumulator else None
            return {"message": {"content": content or None, "tool_calls": tool_calls}}
        else:
            # 非流式模式（现有逻辑）
            response = await self.client.chat(messages, self.tool_defs, stream=False)
            ...
```

---

## P2 - 可观测性与质量

### 25. 结构化调用链路追踪

**问题：** 当前日志散落在各模块，无法追踪一个完整请求的执行路径。

**建议新增 `src/tracing.py`：**

```python
import uuid
import time
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field

logger = logging.getLogger("agent.trace")

# 请求级别的追踪 ID
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
span_id_var: ContextVar[str] = ContextVar("span_id", default="")


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: str
    operation: str
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    status: str = "ok"
    attributes: dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return ((self.end_time or time.time()) - self.start_time) * 1000


class Tracer:
    def __init__(self):
        self._spans: list[Span] = []

    def start_trace(self, operation: str) -> str:
        tid = str(uuid.uuid4())[:12]
        trace_id_var.set(tid)
        return self.start_span(operation, trace_id=tid)

    def start_span(self, operation: str, trace_id: str = None) -> str:
        tid = trace_id or trace_id_var.get()
        sid = str(uuid.uuid4())[:8]
        pid = span_id_var.get()
        span_id_var.set(sid)

        span = Span(trace_id=tid, span_id=sid, parent_id=pid, operation=operation)
        self._spans.append(span)
        logger.debug(f"[{tid}] 开始 {operation} (span={sid})")
        return sid

    def end_span(self, status: str = "ok", **attrs):
        span = self._spans[-1] if self._spans else None
        if span:
            span.end_time = time.time()
            span.status = status
            span.attributes.update(attrs)
            logger.debug(f"[{span.trace_id}] 结束 {span.operation} ({span.duration_ms:.0f}ms, {status})")

    def get_trace_summary(self) -> dict:
        if not self._spans:
            return {}
        trace_id = self._spans[0].trace_id
        total_ms = (self._spans[-1].end_time or time.time()) - self._spans[0].start_time
        return {
            "trace_id": trace_id,
            "total_ms": total_ms * 1000,
            "spans": len(self._spans),
            "errors": sum(1 for s in self._spans if s.status == "error"),
            "details": [
                {"op": s.operation, "ms": s.duration_ms, "status": s.status}
                for s in self._spans
            ]
        }
```

**在 Agent 中集成：**

```python
# agent.py
async def run(self, task, session_id=None):
    tracer = Tracer()
    tracer.start_trace(f"agent.run")

    try:
        for i in range(self.max_iterations):
            tracer.start_span("agent.think")
            response = await self._think(session.messages)
            tracer.end_span()

            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tracer.start_span(f"tool.{func_name}")
                    result = await self._execute_tool(func_name, func_args)
                    tracer.end_span(status="ok" if "错误" not in result else "error")
    finally:
        summary = tracer.get_trace_summary()
        logger.info(f"请求追踪: {json.dumps(summary, ensure_ascii=False)}")
```

---

## 附录：目录结构对比

### 当前项目结构

```
src/
├── agent.py              # Agent 核心
├── agent_session.py      # 会话管理
├── cache.py              # 缓存
├── cmd_handler.py        # CLI 命令
├── config.py             # 配置
├── llm.py                # LLM 客户端（单 Provider）
├── main.py               # 入口
├── scheduler.py          # 定时任务
├── storage.py            # SQLite 存储
├── subagent_manager.py   # 子代理管理
├── mcps/
│   └── manager.py        # MCP 管理
├── memory/
│   ├── manager.py
│   ├── archiver.py
│   ├── extractor.py
│   └── tasks.py
├── plugins/
│   ├── base.py
│   ├── manager.py
│   ├── dingtalk/
│   └── webhook/
├── skills/
│   ├── __init__.py
│   └── skill.py
├── tools/
│   ├── __init__.py       # ToolRegistry
│   ├── file.py
│   ├── shell.py
│   ├── subagent.py
│   ├── memory.py
│   └── todo.py
└── utils/
    └── frontmatter.py
```

### 建议优化后的结构

```
src/
├── agent.py              # Agent 核心（增加并行执行、钩子调用）
├── agent_session.py      # 会话管理（增加上下文压缩）
├── cache.py              # 缓存
├── cmd_handler.py        # CLI 命令（增加 /usage 等）
├── config.py             # 配置
├── main.py               # 入口（增加 -p/--output-format）
├── scheduler.py          # 定时任务
├── storage.py            # SQLite 存储
├── subagent_manager.py   # 子代理管理
├── usage.py              # [新增] Token 与成本追踪
│
├── hooks/                # [新增] 钩子系统
│   ├── __init__.py
│   ├── manager.py
│   └── types.py
│
├── llm/                  # [重构] 多 Provider 支持
│   ├── __init__.py
│   ├── base.py           # 抽象基类
│   ├── openai.py         # OpenAI 兼容
│   ├── anthropic.py      # Anthropic（可选）
│   └── client.py         # 统一客户端
│
├── mcps/
│   └── manager.py        # MCP 管理（增加 HTTP 传输）
│
├── memory/
│   ├── manager.py
│   ├── archiver.py
│   ├── extractor.py
│   └── tasks.py
│
├── permissions/          # [新增] 权限系统
│   ├── __init__.py
│   ├── checker.py
│   ├── modes.py
│   └── rules.py
│
├── plugins/
│   ├── base.py           # 增强扩展点
│   ├── manager.py        # 支持注册命令/钩子/工具
│   ├── dingtalk/
│   └── webhook/
│
├── skills/
│   ├── __init__.py
│   └── skill.py
│
├── tools/
│   ├── __init__.py       # ToolRegistry（增加 Pydantic 验证）
│   ├── base.py           # [改进] 支持 input_model
│   ├── file.py           # [改进] Pydantic 参数
│   ├── shell.py          # [改进] Pydantic 参数
│   ├── subagent.py
│   ├── memory.py
│   └── todo.py
│
├── utils/
│   └── frontmatter.py
│
tests/                    # [新增] 测试
├── conftest.py
├── unit/
│   ├── test_tools.py
│   ├── test_llm.py
│   ├── test_subagent.py
│   ├── test_memory.py
│   ├── test_storage.py
│   └── test_permissions.py
└── integration/
    ├── test_agent_loop.py
    └── test_mcp.py

.github/                  # [新增] CI/CD
└── workflows/
    └── ci.yml

pyproject.toml            # [新增] 项目配置（ruff/pytest）
```
