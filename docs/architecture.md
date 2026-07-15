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

### 4.2 Session 创建/恢复（当前分散在 2 处）

| 位置                                            | 职责                           | 问题                                   |
| ----------------------------------------------- | ------------------------------ | -------------------------------------- |
| `agent.py:_run_impl` (line 625)               | 所有 Agent 的标准 session 管理 | 完整：创建/恢复/持久化                 |
| `subagent_manager.py:run_subagent` (团队路径) | 团队模式的根 session（已简化） | 仅存储用户输入，`_run_impl` 统一管理 |

### 4.3 Session ID 层级

Session ID 反映的是 **调用栈**，不是 Agent 类型：

```
顶层 session:    uuid4()                             (CLI 启动)
子 session:      {parent_session_id}:{child_name}    (subagent 调用)
```

每次 `agent.run(task, session_id=xxx)` 时：

- 如果 `session_id` 已有消息 → 恢复历史（记忆连续性）
- 如果没有 → 创建新 session

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

## 九、待解决的问题

### 9.1 Hook 注册分散在三处

| 位置                 | 注册事件                                           | 用途                   |
| -------------------- | -------------------------------------------------- | ---------------------- |
| `interactive_mode` | TOOL_START, TOOL_RESULT                            | 终端显示（非团队模式） |
| `run_team_agent`   | TOOL_START, TOOL_RESULT, ROUND_START, LLM_RESPONSE | 团队进度回调           |
| `_forward_hooks`   | CHAT_EVENT, TOOL_START, TOOL_RESULT, ROUND_START   | Web UI 流式显示        |

`_forward_hooks` 和 `interactive_mode` 都注册了 TOOL_START/TOOL_RESULT，但前者只是转发给父 agent，后者才实际显示到终端。

### 9.2 团队执行层过多（已修复）

~~9 层调用链~~ → 现在：

```
main.py → Agent(config_dir=team_dir) → agent.run() → _team_run_impl() → TeamOrchestrator.run()
  → DAG → _run_stage → _create_team_subagent() + agent.run() → _run_impl() [ReAct]
```

已移除 `run_subagent`、`run_team_agent`、`_run_team_orchestrator` 中间层。

### 9.3 工具黑名单（tool_denylist）

### 9.4 工具黑名单（tool_denylist）

`_create_team_subagent` 中通过 `tool_denylist` 过滤掉 `subagent`/`web_search` 等工具。但这只是从 `tool_defs`（LLM 视角）中移除，工具本身仍在 registry 中。LLM 可以通过已知的工具名绕过黑名单。

### 9.5 ask_user 模式不统一

| 位置                           | 设置                                | 原因                 |
| ------------------------------ | ----------------------------------- | -------------------- |
| `orchestrator.py:_run_stage` | `set_ask_user_mode("auto")`       | 团队模式下用户不在线 |
| `main.py:interactive_mode`   | `set_input_handler(_on_ask_user)` | 交互模式下允许输入   |

两种方式通过 contextvars 隔离，运行时值取决于谁最后设置——可能互相干扰。

### 9.6 进度回调链路过长

```
_run_stage → tool_callback λ → _cb (self.progress_callback)
  → _team_progress (main.py) → _write() → 终端
```

λ 嵌套 λ，且 `_cb` 是 orchestrator 的 `self.progress_callback`，但 `tool_callback` 却是 `_run_stage` 本地创建的 λ，两者通过参数传递。调试困难。

## 十、建议重构方向

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
