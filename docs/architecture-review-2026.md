# Agent 架构评审 —— 公司级 AI 助手适配度

> 日期：2026-09-06 ｜ 对象：`agent/`（执行引擎 + Web UI）
> 方法：代码通读 + 运行数据盘点 + 真实环境流式抓包验证。结论与建议见文末。

## 0. TL;DR

**可以干活，但还不是“放心服务全公司”的状态。**

优点：功能纵深很强（工具/技能/子代理/团队/记忆/学习/RBAC/沙箱/审计/用量/多用户隔离/流式），
会话模型和 DB 隔离近期已重构到位。
风险：巨型单文件/单 Agent 巨类 + 单进程单事件循环 + **默认未开启多用户隔离池** +
测试集损坏 + 交互短板（Web 端无法在运行中“追问/确认”）。要“正式全公司可用”，
需要一次目标明确的演进（网关 + worker 调度 + 分域重构 + SSO/容量/监控/测试补齐）。

## 1. 规模与形态

| 项 | 数值 |
|---|---|
| 源码 | 122 个 Python 文件 ≈ 3.0 万行 |
| 巨型文件 | `web/server.py` 1960、`storage/storage.py` 1117、`cmd_handler.py` 958、`agent/core.py` 910、`team/orchestrator.py` 866、`agent/subagent.py` 759、`agent/loop.py` 685、`agent/session.py` 644 |
| TODO/FIXME/临时 | 83 处 |
| 技术栈 | Python asyncio · OpenAI SDK · SQLite(WAL) · FastAPI · Vue3 |
| 运行形态 | 单进程单 asyncio 事件循环承载全部渠道（Web/CLI/钉钉/飞书/webhook/定时）+ 可选 per-user Worker 池 |

## 2. 现状架构（简）

```
渠道层: MessageRouter(channel,...) — Web/CLI/DingTalk/Feishu/Webhook/Scheduler/Autonomous
执行层: Agent.run() — root(单例)或 per-user worker
         ├─ ReAct loop(loop.py) + reflective + team(orchestrator)
         ├─ 工具层: BuiltinTool/Skill/Subagent(线程 <对话>#<agent>)/MCP/Plugin
         └─ Hook 事件(流式 SSE / 日志) + RBAC + Sandbox + Tracer
存储:   SQLite(config/data.db) — messages(user/channel/conversation_id)/usage/memory/rbac/kanban…
前端:   frontend/(Vue3)→ static_vue; 页面 Chat/会话/用量/监控/日志/看板/…
```

## 3. 已经不错的部分（公司级底子）

1. **会话即对话（Conversation-first）**：`conversation_id` 一级实体 + 确定性线程
   `<conv>#<agent>`；列表/续聊/审计按对话聚合；`core.run` 自动 DB 恢复历史。
2. **多用户数据隔离**：消息/用量按 `user_id(web:{uid})`；记忆按 `owner_id`；
   会话 API 按 owner/admin 双重校验；子代理按 `(对话, agent)` 隔离。
3. **安全骨架**：JWT + RBAC(工具/子代理在 executor 强制生效) + 路径沙箱 +
   shell 校验 + 登录限流 + 服务令牌。
4. **审计/用量/可观测**：全量消息落盘含审计列、admin 大盘与线程下钻、P50/P95、
   个人用量；DB 迁移幂等自愈。
5. **能力纵深**：40+ 内置工具、技能生命周期、子代理/团队流水线、记忆/自学习、
   上下文 4 层压缩（面向 prompt cache）、沙箱、可插拔 MCP/插件。
6. **近端修复**：流式逐 token + 工具/子代理过程可视化 + `keep-alive` 保活。

## 4. 混乱与风险点（按影响排序）

### 4.1 单进程单事件循环 = 单点 + 容量天花板
- 所有渠道共用一个进程/事件循环；任意长任务、插件卡顿都可能拖慢全体。
- 没有横向扩展/分片；worker 池仍是进程内对象，单进程内存上限决定并发量。
- 长连接(SSE/日志流/钉钉长连)叠加高并发时会争抢同一事件循环。

### 4.2 **默认未启用多用户运行隔离**
- `AGENT_WEB_POOL_SIZE` 默认 0：所有 web 用户共用一个 root Agent 实例，
  共享 root `workspace/`（文件互见）与实例状态。
- 我们做的数据层隔离(conversation/owner/记忆)在**不开池**时依然有效，但
  **文件与实例级隔离需要开池**才成立。公司化部署必须开池，且需容量评估
  （每个 worker = 一次完整 Agent 初始化：工具/MCP/技能/子代理管理器）。

### 4.3 巨型文件与职责混装（可维护性/演进风险）
- `web/server.py`(1960)：登录鉴权、RBAC、会话、任务、看板、记忆、admin、
  workspace、webhook、日志流、SSE 协议、静态托管全挤在一个 `_setup_routes()` 闭包森林。
- `agent/core.py`(910)：prompt 分层、记忆、子代理、团队、权限、MCP、task、plan 模式等
  多重职责聚集；run() 一次要处理“顶层 vs worker vs 子代理”三种语义分支。
- `agent/loop.py` 内 react/reflective/team 三个 run_impl 高度相似、有重复。
- `agent/session.py` 同时承担上下文压缩 4 层 + 会话管理 + 落盘。

### 4.4 概念重叠
`ChatSession(Web 内存)` / `AgentSession(运行上下文)` / `conversation_id(用户对话)` /
`thread(子会话)` 四者并存；展示/内存/持久化三条路径靠命名与散落 API 对齐，
新增功能时容易在“到底该操作哪个”上踩坑（近期已收敛，但代码注释/文档仍有旧称）。

### 4.5 Web 端“运行中交互”缺失
- Web 流式只能单向输出；`ask_user` 在容器无 TTY 时自动回默认值，
  Permission 默认 `auto` 直接放行写操作 → 员工对话中无法确认危险动作/补充信息。
- 需要 SSE 双向（请求内暂停并回传用户回答/审批），这是公司级体验的关键短板。

### 4.6 可靠性/运维
- SQLite 单文件、WAL 单机；无备份策略/保留期（仅在部署清理时手拷 .bak）。
- 长任务进程重启即中断（有 DB 恢复但无任务续跑调度）。
- scheduler/autonomous 绑定单 agent 实例，全公司复用，缺少每用户独立调度与配额。
- 无健康/存活接口之外的可观测告警、无 rate-limit 细化（登录限了、对话未按用户限流）。

### 4.7 测试与文档
- `tests/` 一批用例仍引用旧扁平路径(`agent_session.py`/`storage.py` 顶层导出)，
  在普通环境 collect 即失败 → 回归保障缺失。
- 文档/代码命名漂移(旧 `src/agent.py` 等)已修复一部分，需建立“改码即改档”检查。

### 4.8 其它积弊
- 插件文件偏大且含业务逻辑(feishu 575 / dingtalk 507)；scheduler 在插件与
  autonomous 两处有相似接线。
- `webhook_api` 任务为内存 dict，重启即丢（需回放 DB）。
- tracing(JSONL)默认关闭；实例级 tracer 在“同一 agent 内并发 run”时 span 栈会串。
- 默认 `admin/admin123`、JWT secret 落 `config/jwt_secret`——上线需改口令/注入 secret。

## 5. 目标架构（演进方向）

```
[桌面端/网关(BFF)] ── Bearer/SSO ──► [Agent 接入(单端口)]
                                        │  Authn/JWT→身份
                                        ▼
                           [调度层 Dispatcher/Queue]
                             ├ per-user Worker 池(进程内, 现 worker_pool)
                             └ (远期) 无状态分发→多个 Worker 进程/容器
                                        ▼
                        [能力平面] 工具/技能/子代理/团队/MCP/插件
                                        ▼
                    [持久化: DB/消息/用量/审计] + [可观测: metrics/trace/logs]
```
关键动作（对应当前文件）：
- 把 `web/server.py` 按域拆成 FastAPI Routers：`auth` / `chat+sse` / `sessions` /
  `admin·monitor` / `usage` / `workbench(kanban·scheduler·memory·agents)` /
  `webhook`——每个路由文件独立职责，共享 `Service` 层。
- 抽 `ConversationService`（对话生命周期/恢复/归属）与 `SessionRuntime`
  （运行上下文），UI 只与 Conversation 交互。
- run 语义收敛：把“顶层/worker/子代理/团队”差异收进一个 `RunDispatcher`，
  减少 `core.run` 分支与 loop 三份重复。
- Worker 池随 deploy 默认开启（改 Dockerfile/入口 env），并加容量保护。
- 加 SSE 双向(ask/permission)、按用户/角色限流、长任务落库可续跑。
- 修 tests 引用 → 用根 AGENTS 约定的命令跑绿；补关键路径 e2e。

## 6. 是否“满足全公司工作助手”——逐项判定

| 需求 | 现状 | 差距 |
|---|---|---|
| 多人在线并发 | ✅ 数据隔离 + 会话隔离；⚠️ 需开池 & 单进程上限 | 开池压测、容量/横向 |
| 权限隔离 | ✅ RBAC 工具/子代理强制 + owner 过滤 + 记忆/消息隔离 | 文件隔离依赖开池；网关 SSO 身份映射 |
| 会话/续聊/上下文 | ✅ 对话优先 + DB 恢复 + 压缩 | — |
| 能力广度(工单/运维/代码/问答) | ✅ 工具+技能+子代理+团队+记忆+RAG(可选) | 按部门开通 & 内容治理 |
| 审计合规 | ✅ 全量落盘+用量+线程下钻 | 保留期/防篡改/导出流程 |
| 运行可观测 | ✅ admin 大盘(P50/P95/在线) | 指标化/告警 |
| 运维可靠性 | ⚠️ 单点单库 | 备份、健康探针、灰度回滚、多实例 |
| 交互 | ⚠️ 单向流式 | **SSE 双向追问/审批** |
| 成本治理 | ✅ 用量/配额预留 | 与 router 打通、按部门预算 |

结论：**具备成为公司级 AI 助手的主体能力与数据隔离/审计底子**；
短板集中在“运行形态(单点/默认隔离不足)、维护(巨型文件/测试)、交互(追问审批)、
运维(多实例/备份/告警)”。按 §5 演进，两到三个迭代内可达到“可放心全公司运行”。

## 7. 建议顺序（M0 已部分完成）

- M1（现在，多数已完成）：worker 池默认开 + 容量；跑绿测试与 e2e；SSE 双向 ask/审批；
  对话/用量/审计的网关(BFF/SSO)对接。
- M2：按域拆 Routers 与 Service；run 语义收敛；长任务持久化续跑；限流/配额。
- M3：多进程/多实例部署(共享存储)，指标+告警+备份/恢复制度，审计导出闭环。
