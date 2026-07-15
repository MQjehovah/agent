# AI Agent 架构文档

## 一、整体架构

```
┌────────────────────────────────────────────────────────────┐
│                     接入层 (Channel Layer) · MessageRouter   │
│  format_session_id() · route(channel, content, ...)         │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ 渠道 (Channels)                                       │   │
│  │  CLI (interactive_mode) │ Web UI (server.py)          │   │
│  │  DingTalk │ Feishu │ Webhook │ Scheduler              │   │
│  └──────────────────────┬───────────────────────────────┘   │
└─────────────────────────┬──────────────────────────────────┘
                          │ route(channel, ...)
                          ▼
┌────────────────────────────────────────────────────────────┐
│                     消息路由器                               │
│  ├─ cli:      交互模式, ask_user 直接                       │
│  └─ non-cli:  auto 模式, ask_user 返回默认值                │
│                                                             │
│  session_id 格式: {channel}:{unique_id}                     │
│  ├─ cli:{uuid}                                              │
│  ├─ dingtalk:{conv_id}:{sender_id}                          │
│  ├─ feishu:{chat_id}:{user_id}                              │
│  ├─ webhook:{task_id}                                       │
│  └─ web:{uuid}                                              │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
                     Agent.run()
                     _run_impl() / _team_run_impl()
                     ReAct Loop
```

## 二、Agent 模型

### 2.1 Agent 是统一的

整个系统中只有一种 `Agent` 类。`agent.run()` 是唯一的执行入口，根据 Agent 的配置方式决定执行路径：

```
Agent
├── config_dir 指向 PROMPT.md（无 TEAM.md）
│   └── agent.run() → _run_impl() [ReAct Loop]
│
└── config_dir 指向含 TEAM.md 的目录
    └── agent.run() → _team_run_impl() → TeamOrchestrator.run() → DAG
         ├── Agent.run() [成员1] → _run_impl() [ReAct]
         ├── Agent.run() [成员2] → _run_impl() [ReAct]
         └── ...

- 团队不是一种 Agent 类型，而是 Agent 的 **配置属性**——当 `config_dir` 下存在 `TEAM.md` 时，`agent.run()` 自动走 `_team_run_impl()`（编排器），否则走 `_run_impl()`（ReAct 循环）
- 团队成员 Agent 是普通 Agent（无 TEAM.md），走 ReAct 循环
- Agent 可以嵌套：Agent → subagent → Agent → ...，深度不限（`contextvars` 保证并发隔离）

### 2.2 当前实现状态

目前 `main.py` 中团队模式绕过了 `agent.run()`，直接创建 `TeamOrchestrator`。理想实现是让 `Agent.run()` 内部检测 TEAM.md 并自动路由：

```python
# 当前（main.py 中绕过 agent.run）：
orchestrator = TeamOrchestrator(...)
result = await orchestrator.run(task)

# 理想（agent.run 统一入口）：
agent = Agent(config_dir=team_config_dir)
result = await agent.run(task)  # 内部检测到 TEAM.md → 走编排器
```

## 三、ReAct Loop（唯一执行核心）

唯一位置：`src/agent.py:_run_impl()` (line 570)

```
┌──────────────────────────────────────────────────────────┐
│                    ROUND START                            │
│   HookEvent.ROUND_START → tool_callback → 终端 spinner    │
├──────────────────────────────────────────────────────────┤
│ 1. 上下文压缩 (4 层渐进)                                   │
│    Layer 0: sliding_window (零成本)                       │
│    Layer 1: microcompact (零成本)                         │
│    Layer 2: context_collapse (零成本)                     │
│    Layer 3: LLM summary (有成本，>80% MAX_TOKENS 才触发)  │
│                                                          │
│ 2. 清理孤儿 tool_call                                      │
│ 3. 更新动态 prompt (环境上下文、技能列表、记忆)               │
│ 4. 插入/更新 system prompt 到 messages[0]                  │
├──────────────────────────────────────────────────────────┤
│ 5. _think() → LLMClient.chat(messages, tool_defs)         │
│    多端点 failover + 重试                                  │
├──────────────────────────────────────────────────────────┤
│ 6. 解析 LLM 响应 → content + tool_calls                    │
│ 7. session.add_message("assistant", ...)                  │
│    └─ 持久化到 SQLite                                     │
├──────────────────────────────────────────────────────────┤
│ 8. IF tool_calls:                                         │
│    └─ _execute_tool_calls_parallel()                       │
│       ├─ 权限检查 (PermissionChecker + RBAC)               │
│       ├─ 沙箱拦截                                          │
│       ├─ HookEvent.TOOL_START → 终端显示                   │
│       ├─ 执行: ToolRegistry | SkillManager | MCP | Plugin │
│       ├─ HookEvent.TOOL_RESULT → 终端显示                  │
│       └─ session.add_message("tool", ...)                  │
│    └─ CONTINUE → 下一轮                                    │
├──────────────────────────────────────────────────────────┤
│ 9. IF content (无 tool_calls):                             │
│    └─ BREAK → 返回结果                                     │
├──────────────────────────────────────────────────────────┤
│ 10. ON ERROR: consecutive_errors ≥ 3 → failed             │
│ 11. ON MAX_ITERATIONS (200): 返回部分结果                  │
└──────────────────────────────────────────────────────────┘
```

## 四、Session 生命周期

### 4.1 Session 结构

```
AgentSession (agent_session.py)
├── agent_id, session_id
├── user_id, user_name, role
├── messages: List[ChatCompletionMessageParam]
│    ├── system  (PROMPT.md + 动态上下文)
│    ├── user    (用户输入)
│    ├── assistant (LLM 回复 + tool_calls)
│    └── tool   (工具执行结果)
├── created_at, last_accessed (TTL 过期)
└── agent_manager → AgentSessionManager
```

### 4.2 Session 管理

Session 管理完全由 `agent.py:_run_impl` 统一负责，不存在团队模式的特例：

```
_agent.run(task, session_id=xxx)
  └── _run_impl()  ← 所有 Agent 走这里
       ├─ session_manager.get_or_create(session_id)
       ├─ session_manager.restore_messages()  ← 从 SQLite 恢复历史
       │
       ├─ [如果 team + 复杂任务]
       │   └── _team_run_impl()
       │        └── orchestrator.run()
       │             └── 每个成员 agent.run() → _run_impl()
       │                  └── 各自管理自己的 session
       │
       └─ [简单任务] ReAct Loop
            └─ session.add_message() → 持久化
```

- 所有 Agent（根、leader、成员）共享同一套 session 机制
- session_id 由调用方传入，格式为 `{channel}:{unique_id}`（统一格式）
- 没有"根 session" vs "子 session" 的特殊概念
- 子代理调用的 session_id 格式为 `{parent_session_id}:{child_name}`，反映调用栈

### 4.3 Session ID 格式

### 4.4 持久化

消息通过 `session.add_message()` 存入 SQLite（异步批量写入，1s/10 条合并）。

## 五、团队编排器 (TeamOrchestrator)

### 5.1 流水线构建

```
TEAM.md → member list + pipeline_mode
    │
    ├─ mode=default   → DEFAULT_PIPELINE (7 阶段线性)
    ├─ mode=feedback  → FEEDBACK_PIPELINE (带 dev↔test 反馈循环)
    └─ mode=auto      → LLM 根据 task + members 动态生成
```

### 5.2 DAG 执行

```
while has_pending():
    ready = get_ready_nodes()           # 依赖已完成的节点
    asyncio.gather(*[_execute_stage_node(n) for n in ready])
     → _run_stage(role, stage_id)
        → run_team_agent() → Agent.run() → _run_impl()  [ReAct Loop]
```

### 5.3 阶段结果处理

```
_run_stage 返回
    ├─ ERROR → mark_failed
    ├─ MAXITER → Leader 审核 → 追加迭代或标记失败
    └─ 成功 → mark_completed + 下一阶段
```

### 5.4 Leader 审核

仅 `requirements`/`architecture` 阶段触发。LLM 对产出做 6 维度评分（功能/代码质量/测试/安全/性能/文档），不合格时追加 50 轮迭代。

### 5.5 反馈循环

`testing` 阶段失败时：

1. `parse_test_output()` 解析测试输出
2. 收集失败详情 → 传给 `feedback_to` 目标阶段（如 `implementation`）
3. 重新执行目标阶段 → 重新测试
4. 最多 `max_loops=3` 轮

## 六、Session 与上下文管理

### 6.1 4 层渐进压缩（每轮 ReAct 都执行）

| 层 | 触发条件               | 操作                                            |
| -- | ---------------------- | ----------------------------------------------- |
| 0  | 非 system 消息 > 40 条 | Sliding window，被移除消息序列化为摘要          |
| 1  | **始终执行**     | Microcompact：旧 tool 结果截断到 300 字符       |
| 2  | Token > 65% MAX        | Context collapse：超长文本折叠为头 500 + 尾 300 |
| 3  | Token > 80% MAX        | LLM 摘要：发历史给 LLM 做结构化压缩             |

MAX_CONTEXT_TOKENS = 100k (环境变量可配)

### 6.2 记忆系统

```
MemoryManager (memory/manager.py)
├── 按 user_id 隔离 (WHERE owner_id=?)
├── 分类: preference, key_info, todo, failure_lesson, correction, reflection, knowledge
├── 读取: load_memory() → 按组去重 + 每组限 5 条 + 每条截断 200 字符
│
├── Learner (learning/learner.py)
│   ├── 反思: agent.run() 完成后后台调用 LLM 提取经验
│   ├── 纠正: 检测用户纠正信号 → 存入 correction
│   └── 每日提取: 调用 MemoryCurator 做通用知识提炼
│
└── MemoryCurator (memory/curator.py)
    └── 将用户记忆泛化为全局知识 (经审批后保存)
```

## 七、Hook 系统

### 7.1 事件类型

| 事件                 | 触发时机            | 用途                   |
| -------------------- | ------------------- | ---------------------- |
| `ROUND_START`      | 每轮 ReAct 开始前   | 报告上下文大小、迭代数 |
| `LLM_RESPONSE`     | LLM 返回内容后      | 显示 LLM 推理过程      |
| `TOOL_START`       | 工具执行前          | 终端显示工具调用       |
| `TOOL_RESULT`      | 工具返回后          | 终端显示工具结果       |
| `CHAT_EVENT`       | 流式模式 token 到达 | Web UI 实时显示        |
| `AGENT_START/STOP` | Agent 开始/结束     | 生命周期事件           |
| `SUBAGENT_*`       | 子代理事件（转发）  | Web UI 子代理跟踪      |

### 7.2 并发隔离

`contextvars.ContextVar` 实现 run_id 隔离：

- 每个 `agent.run()` 分配唯一 run_id
- Hook 注册时可指定 run_id（全局回调不指定）
- Web UI 订阅特定 run_id 的流式事件
- 并发请求互不干扰

## 八、工具系统

### 8.1 工具发现

`ToolRegistry.auto_discover()` 扫描 `src/tools/*.py`，实例化所有 `BuiltinTool` 子类。

### 8.2 工具执行链

```
_execute_tool_safe(name, args)
  ├─ PermissionChecker.check() → DEFAULT 模式需要确认
  ├─ RBACManager.check_tool()   → 角色权限
  ├─ Sandbox intercept          → 沙箱重定向
  ├─ HookEvent.TOOL_START       → 终端/Web UI
  ├─ Plugin on_pre_tool_call    → 插件拦截
  ├─ _execute_tool():
  │   ├─ "subagent" → SubagentManager.run_subagent()
  │   ├─ ToolRegistry → BuiltinTool.execute()
  │   ├─ SkillManager → execute_skill
  │   ├─ MCP → call_tool()
  │   └─ Plugin → execute_tool()
  ├─ HookEvent.TOOL_RESULT
  └─ Plugin on_transform_tool_result
```

## 九、当前状态与已知问题

### 9.1 接入层统一（已完成）

新增 `src/channels/router.py`:

```python
class MessageRouter:
    """统一消息路由：所有渠道通过此路由器调用 agent.run()
    
    - route(channel, content, ...) — 非 CLI 渠道自动 set_ask_user_mode("auto")
    - format_session_id(channel, *parts) — 标准化 session ID
    """
```

所有 6 个渠道（CLI、Web、DingTalk、Feishu、Webhook、Scheduler）统一通过 `router.route()` 调用 agent。

### 9.2 Session ID 格式统一（已完成）

| 渠道 | 旧格式 | 新格式 |
|------|--------|--------|
| CLI | `uuid4()` | `cli:{uuid}` |
| DingTalk | `{conv_id}_{sender_id}` | `dingtalk:{conv_id}:{sender_id}` |
| Feishu | `feishu_{chat_id}_{user_id}` | `feishu:{chat_id}:{user_id}` |
| Webhook | `webhook_{task_id[:8]}` | `webhook:{task_id}` |
| Web | `web_{uuid.hex[:8]}` | `web:{uuid}` |

### 9.3 main.py Bug 修复

`interactive_mode` 中 `run_task()` 函数的作用域层级错误：

```
修复前:
    async def run_task():
        ...setup spinner...          # 只做设置，返回 None
    async def _run():
        return await agent.run()     # 调 agent
        try: ...                     # 死代码（return 后不可达）
```

`_run()` 和 `try/except/finally` 在 `interactive_mode` 函数体级别（`run_task` 之外），`try` 在 `_run()` 的 `return` 之后。用户输入后 `run_task` 只启动 spinner 就结束了，agent 实际从未执行。

```
修复后:
    async def run_task():
        ...setup spinner...
        async def _run():
            return await agent.run()
        try:
            task = asyncio.create_task(_run())
            ...await asyncio.wait(...)...  # 实际等待 agent 完成
        ...
        finally:
            ...                            # 清理
```

`_run()` 定义 + 执行/等待/结果展示全在 `run_task()` 内部，流程正确。

### 9.4 ask_user 模式统一（已完成 via MessageRouter）

| 之前 | 现在 |
|------|------|
| 4 处独立的 `set_ask_user_mode()` 包裹 | 1 处：`MessageRouter.route()` |
| CLI: `set_input_handler()` | CLI: `channel="cli"` 直接交互 |
| 非 CLI: `set_ask_user_mode("auto")` | 非 CLI: 内部自动处理 |

### 9.5 临时目录隔离（已完成）

新增 `agent.temp_dir` 概念：

```
workspace  = 项目目录（用户交付物，审慎写入）
temp_dir   = 系统临时目录（中间产物、下载、实验，用完即弃）
            {system_tmp}/agent_{name}_XXXXXX/
```

实现：

| 文件 | 改动 |
|------|------|
| `agent.py` | `initialize()` 中 `mkdtemp` 创建，`cleanup()` 时 `rmtree` 删除 |
| `agent.py:_get_env_context()` | prompt 中加入 `临时目录: {temp_dir}` |
| `tools/shell.py` | 默认 CWD 从 `workspace` 改为 `temp_dir` |
| `tools/__init__.py` | `is_path_allowed` 同时允许 `workspace` 和 `temp_dir` |
| `tools/__init__.py` | `ToolRegistry` 新增 `temp_dir` 属性，自动传播到所有工具 |

效果：

- agent 执行 shell 命令默认在临时目录，不会无意识写文件到项目目录
- 写 `workspace` 需显式指定路径（`file_operation` 的 `path` 参数）
- 临时目录在 agent `cleanup()` 时自动清理
- 操作系统临时目录本身也有定期清理策略

### 9.6 Hook 注册分散在三处

| 位置                 | 注册事件                                           | 用途                   |
| -------------------- | -------------------------------------------------- | ---------------------- |
| `interactive_mode` | TOOL_START, TOOL_RESULT, ROUND_START, SUBAGENT_RESULT | 终端显示             |
| Web SSE stream     | CHAT_EVENT, TOOL_START, TOOL_RESULT, SUBAGENT_*      | Web UI 流式显示       |
| `orchestrator.py`  | TOOL_START, TOOL_RESULT (内部)                       | 团队进度回调          |

### 9.7 工具黑名单（tool_denylist）

`_create_team_subagent` 中通过 `tool_denylist` 过滤掉 `subagent`/`web_search` 等工具。但这只是从 `tool_defs`（LLM 视角）中移除，工具本身仍在 registry 中。LLM 可以通过已知的工具名绕过黑名单。

### 9.8 进度回调链路过长

```
_run_stage → tool_callback λ → _cb (self.progress_callback)
  → _team_progress (main.py) → _write() → 终端
```

λ 嵌套 λ，且 `_cb` 是 orchestrator 的 `self.progress_callback`，但 `tool_callback` 却是 `_run_stage` 本地创建的 λ，两者通过参数传递。调试困难。

## 十、已完成的重构

| 重构 | 状态 | 备注 |
|------|------|------|
| MessageRouter 接入层统一 | ✅ | `src/channels/router.py` |
| Session ID 格式统一 | ✅ | `{channel}:{unique_id}` |
| ask_user 模式统一 | ✅ | 4处 → 1处 MessageRouter |
| 临时目录隔离 | ✅ | `agent.temp_dir` |
| main.py bug 修复 | ✅ | `run_task` 作用域层级 |
| 团队执行层简化 | ✅ | 移除 run_team_agent |

## 十一、建议重构方向

### Hook 分层

```
现在:  三层各自注册钩子
改为:  Agent 层统一注册所有钩子
       团队进度回调通过 progress_callback 参数传递（已实现）
       终端显示通过 hook 消费端处理（不分注册端）
```

```
现在:  9 层调用链
改为:  去掉 run_team_agent 中间层
       _run_stage 直接创建 Agent 并调用 agent.run()
       run_team_agent 的 hook 注册移到 orchestrator 层
```
