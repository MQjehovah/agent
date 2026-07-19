# AI Agent 开发设计文档

## 一、项目概述

多渠道 AI Agent 系统，支持 CLI / Web UI / 钉钉 / 飞书 / Webhook / 定时任务 6 种接入方式。核心能力：ReAct 推理循环、子代理委派、团队流水线编排、知识库检索、自主任务执行、技能工作流、插件扩展。

**技术栈**：Python 3.11+ / asyncio / OpenAI SDK / SQLite / FastAPI / Vue 3

**代码规模**：88 个 Python 文件，~16,700 行

## 二、整体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                        接入层 (Channel Layer)                      │
│   MessageRouter.route(channel, content, ...)                       │
│   ┌──────────┬──────────┬──────────┬──────────┬──────────┐       │
│   │  CLI     │  Web UI  │ DingTalk │  Feishu  │ Webhook  │ ...    │
│   └────┬─────┴────┬─────┴────┬─────┴────┬─────┴────┬─────┘       │
└────────┼──────────┼──────────┼──────────┼──────────┼─────────────┘
         │          │          │          │          │
         └──────────┴──────────┴──────────┴──────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                        Agent 执行引擎                              │
│  Agent.run() → reactor.run_impl() [ReAct Loop]                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ 上下文压缩 → think() → 解析响应 → 工具执行 → 下一轮          │ │
│  └─────────────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │                      工具执行层                               │ │
│  │  PermissionChecker → Sandbox → HookEvent → 执行 → HookEvent  │ │
│  │  ├─ ToolRegistry (file/shell/grep/edit/web/...)             │ │
│  │  ├─ SkillManager (structured workflows)                     │ │
│  │  ├─ AgentFactory (创建 Agent 实例，个人/团队统一入口)         │ │
│  │  ├─ MCPManager (外部工具服务)                                │ │
│  │  └─ PluginManager (webhook/feishu/kanban/...)               │ │
│  └─────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│   Storage        │ │   Memory         │ │   Retrieval      │
│   SQLite         │ │   记忆+学习      │ │   RAG 知识库     │
│   data.db        │ │                  │ │                  │
└──────────────────┘ └──────────────────┘ └──────────────────┘
```

## 三、核心执行引擎

### 3.1 Agent 类

系统中只有一种 `Agent` 类，`agent.run()` 是唯一执行入口：

- `config_dir` 指向普通 `PROMPT.md` → `reactor.run_impl()` [ReAct 循环]
- `config_dir` 指向含 `TEAM.md` 的目录 → `loop.team_run_impl()` [团队编排]
- Agent 可嵌套（父→子→孙），`contextvars` 保证并发隔离

**文件**：`src/agent/core.py` (Agent 类)、`src/agent/reactor.py` (ReAct 循环)、`src/agent/context.py` (RunContext)、`src/agent/factory.py` (AgentFactory)

### 3.2 ReAct 循环

```
每轮迭代:
  1. 上下文压缩 (4 层渐进，见第二十章)
  2. 清理孤儿 tool_call (防止 API 报错)
  3. 更新动态 prompt (环境/记忆/技能)
  4. _think() → LLMClient.chat(messages, tool_defs)
  5. 解析响应: content + tool_calls
  6. IF tool_calls → 并行执行 → CONTINUE
  7. IF content → BREAK → 返回结果
  8. ON ERROR ≥3 → failed
  9. ON MAX_ITERATIONS(100) → 返回部分结果
```

### 3.3 工具执行链

```
_execute_tool_safe(name, args)
  ├─ PermissionChecker.check()     → DEFAULT 模式需用户确认
  ├─ RBACManager.check_tool()      → 角色权限
  ├─ Sandbox intercept             → 沙箱重定向 (shell/file)
  ├─ HookEvent.TOOL_START          → 终端/Web UI 显示
  ├─ Plugin on_pre_tool_call       → 插件可拦截
  ├─ _execute_tool():
  │   ├─ "subagent"    → SubagentManager
  │   ├─ ToolRegistry  → BuiltinTool.execute()
  │   ├─ SkillManager  → execute_skill
  │   ├─ MCP           → call_tool()
  │   └─ Plugin        → execute_tool()
  ├─ 工具结果智能压缩 (见第二十章)
  ├─ HookEvent.TOOL_RESULT
  └─ Plugin on_transform_tool_result
```

### 3.4 并行工具调用

LLM 一次返回多个 tool_call 时，通过 `asyncio.gather` 并行执行：

```python
tasks = [asyncio.create_task(_run_one(tc)) for tc in tool_calls]
results = await asyncio.gather(*tasks, return_exceptions=True)
```

## 四、LLM 客户端

### 4.1 多端点 Failover

```python
LLMClient(endpoints=[...])
  ├─ 按顺序尝试每个端点
  ├─ 单端点重试: MAX_RETRIES_PER_ENDPOINT=3
  ├─ 多端点模式: 每端点 1 次重试后切换
  └─ max_retries=0 (SDK 层不重试，全部由应用层处理)
```

### 4.2 重试策略

| 异常类型 | 重试 | 延迟 |
|---------|------|------|
| RateLimitError | ✅ | 60s 冷却 |
| APIConnectionError | ✅ | 指数退避 2^n |
| APITimeoutError | ✅ | 指数退避 2^n + 5s |
| APIError ≥500 | ✅ | 指数退避 |
| 其他 | ❌ | — |

### 4.3 超时配置

```
LLM_TIMEOUT       = 300s (read timeout)
LLM_CONNECT_TIMEOUT = 30s  (connect timeout)
```

### 4.4 Prompt Caching

为 system 消息添加 `cache_control: {type: ephemeral}`，支持 deepseek/gpt-4/claude。

**文件**：`src/llm/client.py`

## 五、会话管理

### 5.1 结构

```
AgentSession
├── agent_id, session_id
├── user_id, user_name, role
├── messages: List[ChatCompletionMessageParam]
│   ├── system    (PROMPT.md + 动态上下文)
│   ├── user      (用户输入)
│   ├── assistant (LLM 回复 + tool_calls)
│   └── tool      (工具执行结果)
├── created_at, last_accessed (TTL 过期)
└── AgentSessionManager (TTL 清理 + 容量限制)
```

### 5.2 Session ID 格式

```
{channel}:{unique_id}
├── cli:{uuid}
├── dingtalk:{conv_id}:{sender_id}
├── feishu:{chat_id}:{user_id}
├── webhook:{task_id}
└── web:{uuid}
```

子代理调用：`{parent_session_id}:{child_name}`

### 5.3 持久化

消息通过 `session.add_message()` 存入 SQLite，异步批量写入。

**文件**：`src/conversation/session.py`

## 六、工具系统

### 6.1 内置工具

| 工具 | 文件 | 说明 |
|------|------|------|
| `file_operation` | `tools/file.py` | 读/写/追加/删除/列目录，50K 字符截断 |
| `shell` | `tools/shell.py` | 命令执行，stdout 10K 截断 |
| `grep` | `tools/grep.py` | 正则搜索，50 条匹配上限 |
| `glob` | `tools/glob.py` | 文件模式匹配，100 条上限 |
| `edit` | `tools/edit.py` | 精确字符串替换 |
| `code_preview` | `tools/code_preview.py` | 代码片段预览 |
| `web_search` | `tools/web.py` | 多引擎搜索 (SearXNG/Tavily/Serper/Bing) |
| `web_fetch` | `tools/web.py` | 网页抓取 |
| `subagent` | `tools/subagent.py` | 子代理委派（实际逻辑在 agent.py） |
| `memory` | `tools/memory.py` | 记忆读写 |
| `todowrite` | `tools/todo.py` | 任务列表管理 |
| `ask_user` | `tools/ask_user.py` | 向用户提问（CLI 交互/auto 模式） |
| `task_create/list/get/cancel` | `tools/task.py` | 异步任务管理 |
| `knowledge_search` | `retrieval/__init__.py` | RAG 知识库检索 |

### 6.2 工具注册

```python
ToolRegistry.auto_discover()  # AST 扫描 src/tools/*.py，实例化 BuiltinTool 子类
```

### 6.3 工具定义传给 LLM

所有工具的 JSON Schema 作为 `tools` 参数传给 LLM，决定 LLM 可调用的工具集。团队子代理通过 `tool_denylist` 过滤工具。

**文件**：`src/tools/` (13 个工具文件)

## 七、子代理与团队编排

### 7.1 个人子代理 / 团队成员

**系统中只有一种 Agent 类**，没有 Subagent 概念。个人子代理和团队成员都是 `Agent` 实例，区别仅在于 `config_dir` 指向不同的 PROMPT.md。

```
AgentFactory (agent/factory.py)
  ├─ scan() — 扫描 config/agents/ 加载所有模板
  ├─ create(template) — 创建个人 Agent（复用活跃实例）
  ├─ create_team_member(team, role) — 创建团队成员 Agent
  └─ get_subagent_prompt() — 生成子代理列表提示词
```

### 7.2 团队编排

`config/agents/AI开发团队/` 包含 `TEAM.md` + `agents/` 子目录（7 个角色）。

```
TeamOrchestrator.run(task)
  ├─ 构建流水线 (LLM 动态生成 / 固定模板)
  ├─ DAG 执行引擎
  │   └─ 并行执行就绪节点
  │       └─ _run_stage(role) → AgentFactory.create_team_member() → Agent.run()
  ├─ 反馈循环 (testing 失败 → implementation 修复 → retest)
  └─ _build_report() → 执行报告
```

**文件**：`src/agent/factory.py` (AgentFactory)、`src/team/` (orchestrator/context/dag/feedback/pipeline_builder)

## 八、Hook 事件系统

### 8.1 事件类型

| 事件 | 触发时机 | 用途 |
|------|---------|------|
| `ROUND_START` | 每轮 ReAct 开始 | 上下文统计、迭代数 |
| `LLM_RESPONSE` | LLM 返回后 | 流式输出、终端显示 |
| `TOOL_START` | 工具执行前 | 终端/Web UI 显示 |
| `TOOL_RESULT` | 工具返回后 | 终端/Web UI 显示 |
| `CHAT_EVENT` | 流式 token 到达 | Web UI 实时显示 |
| `AGENT_START/STOP` | Agent 生命周期 | 状态追踪 |
| `SUBAGENT_*` | 子代理事件转发 | 嵌套追踪 |
| `SUBAGENT_PROGRESS` | 团队进度 | 阶段/上下文/token 追踪 |

### 8.2 并发隔离

`contextvars.ContextVar` 实现 `run_id` 隔离——每个 `agent.run()` 分配唯一 ID，并发请求互不干扰。

**文件**：`src/hooks/` (types.py, manager.py)

## 九、记忆与学习系统

### 9.1 记忆管理

```
MemoryManager (memory/manager.py)
├── 按 user_id 隔离
├── 分类: preference / key_info / todo / failure_lesson
│        / correction / reflection / knowledge
├── 读取: load_memory() → 按组去重 + 每组限 5 条 + 每条截断 200 字符
└── 存储: SQLite (memories 表)
```

### 9.2 自学习

```
Learner (learning/learner.py)
├── 反思: agent.run() 完成后后台提取经验 → 存入 memory
├── 纠正: 检测用户纠正信号 → 存入 correction
├── 模式追踪: PatternTracker 识别重复任务模式
└── 自动创建: AutoCreator 从模式生成 skill/sub-agent 模板

MemoryCurator (memory/curator.py)
└── 将用户记忆泛化为全局知识 (经审批后保存)
```

**文件**：`src/memory/`、`src/learning/`

## 十、技能系统

### 10.1 技能结构

```
skills/<skill-name>/
├── SKILL.md          # frontmatter (name/description) + 工作流正文
├── references/       # 参考清单 (可选)
├── scripts/          # 脚本 (可选)
└── assets/           # 资源 (可选)
```

### 10.2 生命周期

技能遵循 DEFINE → PLAN → BUILD → VERIFY → REVIEW → SHIP 生命周期。Agent 通过 `skill` 工具加载结构化工作流指引。

### 10.3 意图路由

| 用户意图 | 加载 skill |
|---------|-----------|
| 新功能/新项目 | spec-driven-development |
| 修 Bug | debugging-and-error-recovery |
| 代码审查 | code-review-and-quality |
| 性能优化 | performance-optimization |
| 部署上线 | shipping-and-launch |

**文件**：`src/skills/skill.py`

## 十一、插件系统

### 11.1 插件接口

```python
class BasePlugin(ABC):
    # 生命周期
    async def on_load(self)
    async def on_unload(self)
    # 工具拦截
    async def on_pre_tool_call(self, name, args) → dict | None  # 返回 dict 则拦截
    async def on_transform_tool_result(self, name, result) → str
    # LLM 拦截
    async def on_pre_llm_call(self, messages, tools) → dict | None
    async def on_post_llm_call(self, response)
    # 工具定义
    def get_tool_definitions(self) → list[dict]
```

### 11.2 内置插件

| 插件 | 功能 |
|------|------|
| `dingtalk` | 钉钉机器人消息收发 |
| `feishu` | 飞书机器人消息收发 |
| `webhook` | Webhook 触发任务执行 |
| `kanban` | 看板任务管理 + 定时调度 |
| `scheduler` | Cron 定时任务 |

**文件**：`src/plugins/`

## 十二、知识检索 (RAG)

```
RetrievalTool (knowledge_search)
  ├─ 连接外部 RAG 服务 (config.json: rag.base_url)
  ├─ Token 认证缓存
  ├─ /api/search 查询
  └─ 返回: [{title, content, score, source}, ...]
```

**文件**：`src/retrieval/`

## 十三、MCP 集成

```
MCPManager (mcps/manager.py)
├─ 加载 config/mcp_servers.json
├─ 每个服务器: MCPServerConnection
│   ├─ 启动子进程 / stdio 通信
│   ├─ 健康检查 + 自动重连
│   └─ 工具定义聚合
└─ 工具暴露给 Agent 的 ToolRegistry
```

默认禁用 (`"enabled": false`)，需显式启用。

**文件**：`src/mcps/manager.py`

## 十四、权限系统

### 14.1 权限模式

| 模式 | 行为 |
|------|------|
| `default` | 写操作需用户确认 |
| `auto` | 全部允许（容器环境） |
| `plan` | 只读（规划模式） |

### 14.2 RBAC

```
RBACManager (rbac.py)
├─ 用户身份绑定 (跨平台: 钉钉/飞书/Web)
├─ 角色定义 (config/rbac.json)
├─ 工具权限: check_tool(role, tool_name)
└─ 子代理权限: check_agent(role, agent_name)
```

### 14.3 路径规则

```
PermissionChecker
├─ 允许路径: workspace + temp_dir
├─ 禁止路径: 系统目录、.env
└─ 禁止命令: rm -rf, sudo, format...
```

**文件**：`src/security/permissions/`、`src/security/rbac.py`

## 十五、沙箱

```
SandboxMiddleware (sandbox/__init__.py)
├─ 工具无感知: 拦截在 Agent._sandbox_intercept() 层
├─ 模式:
│   ├─ process: ProcessSandbox (子进程执行)
│   └─ docker: DockerSandbox (容器执行，资源限制)
├─ CommandValidator: 拦截危险命令
└─ PathValidator: 拦截越界路径
```

**文件**：`src/security/sandbox/`

## 十六、存储层

```
Storage (storage.py) — 统一 SQLite
├── data.db (单文件)
├── 连接池
├── 表:
│   ├── messages          (对话历史)
│   ├── eventbus_events   (事件总线)
│   ├── autonomous_goals  (自主任务)
│   ├── kanban_tasks      (看板任务)
│   └── memories          (记忆)
└── 单例: init_storage(workspace, config_dir)
```

**文件**：`src/storage/storage.py`

## 十七、接入层

```
MessageRouter (channels/router.py)
├─ route(channel, content, ...) → agent.run()
├─ format_session_id(channel, *parts) → "{channel}:{id}"
├─ ask_user 模式:
│   ├─ cli: 直接交互
│   └─ non-cli: auto 模式 (返回默认值)
└─ 渠道:
    ├── CLI (interactive_mode)
    ├── Web UI (FastAPI + SSE)
    ├── DingTalk (webhook 收发)
    ├── Feishu (webhook 收发)
    ├── Webhook (HTTP 触发)
    └── Scheduler (cron 定时)
```

**文件**：`src/channels/router.py`

## 十八、自主模式

```
AutonomousLoop (autonomous/loop.py)
  感知 → 规划 → 执行 → 校验 循环
  
  Perceiver: 事件分类 → 目标生成
     ↓
  Planner: LLM 任务分解 → Plan(PlanStep[])
     ↓
  Executor: 逐步执行 (agent.run / 工具调用)
     ↓
  Verifier: LLM 校验目标达成
     ↓
  Reporter: 进度/结果通知 (钉钉/飞书)
  
  EventBus: SQLite 持久化事件队列
  Panel: 自主任务队列管理
```

**文件**：`src/autonomous/` (10 个文件)

## 十九、Web UI

```
WebServer (web/server.py) — FastAPI
├── 对话: POST /api/chat (流式 SSE)
├── 会话: GET/DELETE /api/sessions
├── 任务: CRUD /api/tasks
├── 看板: CRUD /api/kanban
├── 记忆: CRUD /api/memory
├── RBAC: 角色/用户/身份管理
├── Agent 编辑: /api/agents (技能/子代理配置)
├── 日志流: SSE /api/logs/stream
└── JWT 认证
```

前端：Vue 3 + UnoCSS，`src/web/static/`

## 二十、Token 优化

### 20.1 问题

Agent 采用 ReAct 循环，每轮将完整上下文发送给 LLM。单任务 50-100 轮迭代时上下文线性增长，不优化时累计消耗可达 500K-1M token。

### 20.2 优化维度

#### 20.2.1 系统提示词压缩

| 原则 | 做法 |
|------|------|
| 去冗余 | 删除礼貌用语、重复说明 |
| 压缩路由表 | 多列变 2 列 |
| 去示例 | 删除 JSON 调用示例 |
| 静态知识外移 | 通过 `knowledge_search` 按需检索 |

```
压缩前: 3500 字符 ≈ 1000 token
压缩后: 1048 字符 ≈ 300 token
每任务节省: ~700 token × 100 轮 = 70,000 token
```

#### 20.2.2 对话历史 4 层压缩管线

每轮迭代开始时 `AgentSessionManager.compress_if_needed()` 执行：

```
Layer 0: 滑动窗口 (sliding_window)
  触发: 非系统消息 > 10 条
  动作: 保留最近 N 条，滑落消息序列化为摘要
  成本: 零

Layer 1: 工具结果截断 (microcompact)
  触发: 每轮无条件
  动作: 保留最近 5 条工具结果完整，更早的截断到 150 字符
  豁免: skill / ask_user 结果跨轮次需要
  成本: 零

Layer 2: 文本块折叠 (context_collapse)
  触发: token ≥ 65% × MAX (100K)
  动作: 超长文本(>3000字)保留头 500 + 尾 300 字符
  成本: 零

Layer 3: LLM 摘要 (compress_if_needed)
  触发: token ≥ 80% × MAX
  动作: 调用小模型将历史压缩为结构化摘要
  保留: 系统提示 + 摘要 + 最近 8 条消息
  成本: 1 次 LLM 调用
```

**关键设计**：滑动窗口裁剪时保证 tool_call / tool_response 配对完整性（OpenAI API 强制要求）。

#### 20.2.3 工具结果智能压缩

`src/tool_result_compressor.py` 按工具类型注册专用压缩器，替代粗暴的 `result[:3000]` 截断：

| 工具 | 压缩策略 |
|------|---------|
| `subagent` | 提取 status + success，结果文本用 head_tail(65%头+35%尾) |
| `knowledge_search` | 保留 Top 3，每条截断内容 |
| `file_operation` | 保留头尾行 + 行数统计 |
| `grep` | 保留前 N 条匹配（去掉 context 字段） |
| `shell` | stdout 70% + stderr 20% |
| `web_search` | 保留 Top 5 |
| `web_fetch` | head_tail 截断正文 |
| 通用 JSON | 保留关键字段，截断内容字段 |

`_KEEP_FULL = {"skill", "execute_skill", "ask_user", "todo_write"}` — 这些工具结果跨轮次需要，不压缩。

#### 20.2.4 提示词分层与 Prompt Caching

```
┌─────────────────────────────────────┐
│ Static Section (可被 prompt cache)    │  ← 不变，放在最前面
│  - 角色定义、路由规则、工具描述        │
├────── DYNAMIC_BOUNDARY ──────────────┤
│ Dynamic Section (每轮变化)            │  ← 变化，放在后面
│  - 环境上下文、记忆、技能、子代理列表  │
└─────────────────────────────────────┘
```

服务端缓存：system 消息添加 `cache_control: {type: ephemeral}`
本地缓存：相同 (messages, tools, model) 的请求缓存响应

#### 20.2.5 并行工具调用

LLM 一次返回多个 tool_call 时并行执行，3 个工具从 3 轮 LLM 交互减少为 1 轮。

### 20.3 配置参数

```json
{
    "context": {
        "sliding_window_size": 10,
        "sliding_window_summary_max": 6000,
        "tool_result_keep_recent": 5,
        "tool_result_collapse_chars": 150
    }
}
```

| 参数 | 默认值 | 环境变量 | 说明 |
|------|--------|---------|------|
| `sliding_window_size` | 10 | `SLIDING_WINDOW_SIZE` | 滑动窗口保留消息数 |
| `sliding_window_summary_max` | 6000 | `SLIDING_WINDOW_SUMMARY_MAX` | 滑落消息摘要最大字符 |
| `tool_result_keep_recent` | 5 | `KEEP_RECENT_TOOL_RESULTS` | 保留完整结果的条数 |
| `tool_result_collapse_chars` | 150 | `TOOL_RESULT_COLLAPSE_CHARS` | 旧工具结果截断字符数 |
| `MAX_CONTEXT_TOKENS` | 100000 | `MAX_CONTEXT_TOKENS` | 上下文 token 上限 |
| `MAX_TOOL_OUTPUT_CHARS` | 4000 | `MAX_TOOL_OUTPUT_CHARS` | 工具结果全局字符上限 |

配置优先级：环境变量 > config.json > 类默认值

### 20.4 文件索引

| 文件 | 职责 |
|------|------|
| `src/conversation/session.py` | 4 层压缩管线 |
| `src/tools/compressor.py` | 工具结果智能压缩器（原 tool_result_compressor） |
| `src/conversation/prompt.py` | 静态/动态提示词分层 |
| `src/llm/client.py` | Prompt caching + 本地响应缓存 |
| `src/agent/reactor.py` | 压缩管线接入 + 并行工具执行 |
| `src/settings.py` | 上下文压缩参数 |
| `src/llm/tracing.py` | 上下文 token 统计 |
| `config/PROMPT.md` | 压缩后的系统提示词 |

### 20.5 效果估算

以 100 轮迭代任务为例：

| 优化项 | 100 轮累计节省 |
|--------|--------------|
| 系统提示词压缩 | ~70,000 token |
| 滑动窗口 (40→10) | ~200,000 token |
| 工具结果截断 (300→150) | ~375,000 token |
| 工具结果智能压缩 | ~50,000 token |
| **合计** | **~695,000 token** |

## 二十一、配置系统

### 21.1 配置文件

```
config/
├── config.json          # 主配置（从 config.example.json 复制）
├── PROMPT.md            # 根 agent 系统提示词
├── agents/              # 子 agent / 团队定义
├── skills/              # 全局技能
├── mcp_servers.json     # MCP 服务器配置
├── schedules.json       # 定时任务
├── sandbox.json         # 沙箱配置（可选）
├── rbac.json            # RBAC 角色配置（可选）
└── data.db              # SQLite 存储（运行时生成）
```

### 21.2 配置加载链

```
Settings 单例
  ├─ 加载 config/config.json (覆盖默认值)
  ├─ 加载 .env (API key 等敏感信息)
  └─ 环境变量覆盖 (最高优先级)

Config.load_from_env()
  └─ 从 Settings 读取 → 设置类变量

AgentSessionManager.load_config()
  └─ 从 Config 读取 → 设置压缩参数
```

**文件**：`src/settings.py`、`src/config.py`

## 二十二、部署

### 22.1 本地运行

```bash
pip install -r requirements.txt
cp .env.example .env  # 编辑 .env，设置 OPENAI_API_KEY
python src/main.py
```

### 22.2 Docker

```bash
docker build -t agent .
docker run --rm -e OPENAI_API_KEY=sk-... agent
```

端口 8081 暴露（插件/Webhook）。默认 CMD: `python src/main.py --debug`。

### 22.3 目录说明

```
config/    # 配置目录（Docker 挂载）
workspace/ # Agent 工作目录（用户交付物）
logs/      # 日志目录（本地优先，回退 ~/agent/logs）
```
