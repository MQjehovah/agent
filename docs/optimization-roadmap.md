# 优化路线图

> 基于 grok-build (xAI) 对比分析，按优先级排列的优化方案。

## 阶段一：架构现代化（P0，预计 4-6 周）

### 1.1 引入 ACP 协议层

**目标**：将 Agent 执行引擎与 UI/接入层解耦，实现单一 agent 多端服务。

**当前问题**：`Agent.run()` 被 CLI/Web/钉钉/飞书直接调用，无法独立运行。

**实现方案**：

```python
# src/protocol/acp_server.py
class ACPServer:
    """轻量级 JSON-RPC 2.0 服务器，支持 stdio/socket 传输"""
    
    async def handle_request(self, request: dict) -> dict:
        method = request.get("method")
        params = request.get("params", {})
        if method == "session/prompt":
            return await self._handle_prompt(params)
        elif method == "session/cancel":
            return await self._handle_cancel(params)
        # ...
```

**关键设计**：
- 协议定义 → 单一 `sessions: Dict[SessionId, SessionTask]` 管理器
- 每个 session 一个 asyncio.Task，通过 Queue 通信
- CLI/Web/Webhook 统一通过 ACP 客户端调用

**涉及文件**：
- Create: `src/protocol/acp_server.py`
- Create: `src/protocol/acp_client.py`  
- Create: `src/protocol/types.py`
- Modify: `src/main.py`（启动 ACP server）

### 1.2 目标驱动闭环

**目标**：将当前 DAG + 反馈循环升级为 Plan → Execute → Verify → Re-plan 闭环。

**当前问题**：TeamOrchestrator 的反馈循环只有 1 层（实现→测试→修复），缺少规划验证和对抗性审查。

**实现方案**：

```python
# src/orchestrator/goal_loop.py
class GoalOrchestrator:
    """Plan → Execute → Verify → Re-plan 闭环"""
    
    async def run(self, objective: str) -> GoalResult:
        plan = await self.planner.create_plan(objective)
        while not self.is_complete(plan):
            # 执行阶段
            for step in plan.pending_steps:
                result = await self.executor.run(step)
            
            # 验证阶段（对抗性审查）
            verdict = await self.skeptic.verify(plan, results)
            if verdict.needs_replan:
                plan = await self.strategist.replan(plan, verdict)
            elif verdict.blocked:
                await self.strategist.resolve_blocker(plan, verdict)
```

**关键设计**：
- Planner：LLM 生成结构化计划（step → tool → expected outcome）
- Executor：逐步骤执行，记录结果和 diff
- Skeptic：对抗性验证（Classify → 通过/需修改/阻塞）
- Strategist：卡住时重新规划路线
- 支持 checkpoint/restore（每个 step 完成时保存）

**涉及文件**：
- Create: `src/orchestrator/goal_loop.py`
- Create: `src/orchestrator/planner.py`
- Create: `src/orchestrator/skeptic.py`
- Create: `src/orchestrator/strategist.py`
- Modify: `src/subagent_manager.py`（接入 goal loop）

### 1.3 增强 MCP 支持

**目标**：MCP 从默认禁用 → 一等公民，支持多传输和自动恢复。

**当前问题**：仅支持 stdio，默认禁用，无自动重连。

**实现方案**：

```python
# src/mcps/transport.py
class MCPTransport(ABC):
    @abstractmethod
    async def connect(self): ...
    @abstractmethod
    async def send(self, message: dict): ...
    @abstractmethod
    async def receive(self) -> dict: ...

class StdioTransport(MCPTransport): ...
class HTTPTransport(MCPTransport): ...
class SSETransport(MCPTransport): ...
```

```python
# src/mcps/manager.py (增强)
class MCPManager:
    # 自动重连（指数退避 + jitter）
    # OAuth 凭证管理
    # 状态推送（通过 ACP）
    # 多传输支持
```

**涉及文件**：
- Create: `src/mcps/transport.py`
- Modify: `src/mcps/manager.py`
- Modify: `src/main.py`（默认启用 MCP）

---

## 阶段二：基础设施强化（P1，预计 3-4 周）

### 2.1 多源工具聚合 (ToolBridge)

**目标**：统一调度 builtin、MCP、plugin、gateway 工具。

```python
# src/tools/bridge.py
class ToolBridge:
    """多源工具聚合调度器"""
    
    def register_source(self, name: str, tools: List[Tool]):
        self._sources[name] = tools
    
    def get_definitions(self) -> List[dict]:
        return [t.definition for s in self._sources.values() for t in s]
    
    async def execute(self, name: str, args: dict) -> str:
        for source in self._sources.values():
            for tool in source:
                if tool.name == name:
                    return await tool.execute(**args)
        raise ToolNotFoundError(name)
```

### 2.2 分层配置 + 热重载

```python
# src/config/loader.py
class ConfigLoader:
    """环境变量 > CLI > 用户配置 > 项目配置 > 远程配置"""
    
    async def watch(self, callback):
        """文件 watcher 热重载"""
        async for event in FileWatcher(["config/config.json"]):
            if event.is_modified:
                await self.reload()
                await callback(self._merged)
```

### 2.3 沙箱强化

```python
# sandbox/enhanced.py
class EnhancedSandbox:
    """Docker 沙箱 + 网络控制"""
    
    async def execute(self, command: str) -> str:
        # 容器化执行
        # 资源限制 (CPU/内存/网络)
        # 只读文件系统 (除 workspace)
        # 网络策略 (允许/禁止外联)
```

---

## 阶段三：能力扩展（P2，预计 4-6 周）

### 3.1 文件版本追踪

```python
# src/workspace/hunk_tracker.py
class HunkTracker:
    """按 session 追踪文件 diff，支持 rewind"""
    
    def record_change(self, path: str, old_content: str, new_content: str):
        """记录文件变更"""
    
    def get_diff(self, session_id: str) -> List[Hunk]:
        """获取 session 的所有变更"""
    
    async def rewind(self, session_id: str, step: int):
        """回退到指定步骤"""
```

### 3.2 代码索引

```python
# src/indexer/codebase_index.py
class CodebaseIndex:
    """跨 session 共享的代码索引"""
    
    async def index(self, path: str):
        """索引项目代码"""
    
    async def search(self, query: str) -> List[IndexEntry]:
        """语义搜索"""
    
    def get_symbols(self, file: str) -> List[Symbol]:
        """文件符号表"""
```

### 3.3 Computer Use

```python
# tools/computer.py
class ComputerTool(BuiltinTool):
    """本地桌面自动化"""
    
    async def execute(self, **kwargs) -> str:
        action = kwargs.get("action")  # click/type/screenshot/scroll
        # 通过 pyautogui / minicap 实现
```

---

## 四、实施优先级

```
周次 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10| 11| 12| 13| 14|
─────┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┼───┤
P0-1 │███ ███ ███ ███ ███                                     │ ACP 协议层
P0-2 │         ███ ███ ███ ███ ███                            │ 目标驱动闭环
P0-3 │                     ███ ███ ███ ███                    │ MCP 增强
─────┼─────────────────────────────────────────────────────────┤
P1-1 │                         ███ ███ ███                    │ ToolBridge
P1-2 │                             ███ ███ ███                │ 配置热重载
P1-3 │                                 ███ ███ ███            │ 沙箱强化
─────┼─────────────────────────────────────────────────────────┤
P2-1 │                                     ███ ███            │ 文件追踪
P2-2 │                                         ███ ███        │ 代码索引
P2-3 │                                             ███ ███    │ Computer Use
```

## 五、效果评估指标

| 指标 | 当前 | 阶段一目标 | 阶段二目标 | 阶段三目标 |
|------|------|-----------|-----------|-----------|
| 多端并发 | 1（阻塞） | 10+（非阻塞） | 50+ | 100+ |
| 任务完成率 | - | +15% | +30% | +50% |
| MCP 工具数 | 2（默认禁用） | 10+（默认启用） | 50+ | 不限 |
| 沙箱覆盖率 | 20% | 50% | 80% | 95%+ |
| 上下文压缩率 | ~70% | ~80% | ~85% | ~90% |
