# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

这是一个 Python 多智能体系统框架，用于构建企业内部的 AI Agent 系统。主代理"零号员工"负责接收请求并分派给专业子代理（设备运维、IT运维、数字中台、代码审查等）。

## Commands

### 运行 Agent

```bash
# 交互模式
python src/main.py

# 单任务模式
python src/main.py -t "任务描述"

# 指定工作目录
python src/main.py -w workspace

# 调试模式
python src/main.py --debug

# 禁用定时任务
python src/main.py --no-scheduler

# 禁用插件
python src/main.py --no-plugins
```

### Docker 部署

```bash
docker build -t agent .
docker run -p 8081:8081 agent
```

### 交互模式命令

在交互模式中可使用以下命令：
- `/prompt` - 查看系统提示词
- `/tools` - 列出可用工具
- `/skills` - 列出可用技能
- `/sessions` - 列出所有会话
- `/session <id>` - 查看指定会话详情
- `/messages` - 查看当前会话消息历史
- `quit/exit/q` - 退出

## Architecture

### 核心组件 (src/)

| 模块 | 职责 |
|------|------|
| `main.py` | 入口点，CLI交互模式，信号处理，资源清理 |
| `agent.py` | Agent类，工具注册，MCP连接，子代理管理，记忆初始化 |
| `llm.py` | OpenAI SDK封装，日志记录请求/响应 |
| `storage.py` | SQLite存储会话消息 |
| `scheduler.py` | APScheduler定时任务管理 |

### 工具系统 (src/tools/)

内置工具：`todo`, `file`, `subagent`, `memory`, `shell`

工具通过 `ToolRegistry` 注册，支持动态注册/注销。

### 技能系统 (src/skills/)

每个技能是一个目录，包含 `SKILL.md` (YAML frontmatter + 提示词模板)。技能通过 `execute_skill` 工具调用。

### MCP服务器 (src/mcps/)

MCP服务器配置在 `workspace/mcp_servers.json`。每个配置包含：
- `name`: 服务名称
- `command`: 启动命令
- `args`: 参数列表
- `env`: 环境变量
- `enabled`: 是否启用

实现位于 `mcp_server/src/`：
- `device_ops.py` - 设备运维API
- `terminal.py` - WebSocket终端交互
- `default.py` - 数据库查询和邮件

### 插件系统 (src/plugins/)

插件继承 `BasePlugin`，实现 `start()`, `stop()`, `_load_config()`。现有插件：
- `dingtalk` - 钉钉消息接入
- `webhook` - HTTP接口

### 记忆系统 (src/memory/)

三层记忆：
- **会话记忆**: SQLite存储，按session_id
- **每日记忆**: `workspace/memory/agents/<agent_id>/daily/<date>.md`
- **长期记忆**: `workspace/memory/memory.md`

每日凌晨自动提取会话摘要归档。

### Workspace结构

```
workspace/
├── PROMPT.md              # 主代理提示词（零号员工）
├── mcp_servers.json       # MCP服务配置
├── schedules.json         # 定时任务配置
├── agents/                # 子代理目录
│   ├── 设备运维/PROMPT.md
│   ├── IT运维/PROMPT.md
│   ├── 数字中台/PROMPT.md
│   └── 代码审查/PROMPT.md
├── skills/                # 技能目录
└── memory/                # 记忆存储
```

### Agent工作流

1. `Agent.initialize()` 加载：
   - PROMPT.md (frontmatter解析name/description)
   - skills目录下所有SKILL.md
   - mcp_servers.json配置的服务
   - agents目录下的子代理模板
   - 记忆上下文

2. `Agent.run(task)` 执行：
   - 创建/复用AgentSession
   - 循环调用 `_think()` (LLM)
   - 执行tool_calls → `_execute_tool()`
   - 直到无tool_calls或达到max_iterations

### PROMPT.md格式

```markdown
---
name: 代理名称
description: 描述
---
# 系统提示词内容
```

### SKILL.md格式

```markdown
---
name: 技能名称
description: 描述
tools: [tool1, tool2]
---
# 技能提示词模板
```