# P2 · 公司级多用户运行隔离与可观测设计（实施记录）

> 状态：已实施（Web Worker 池、用户级 workspace、会话 DB 恢复、个人用量、接口鉴权收敛）
> 范围：`agent/` 子系统的 Web 入口与运行态隔离；不涉及 dashboard/网关侧改动。

## 一、目标

全公司共用的在线智能体需要：

1. 真并发：多员工同时对话互不影响（实例状态 / tracer / 上下文不串）；
2. 权限隔离：消息、会话、记忆、文件、用量都按人隔离；
3. 审计可观测：谁、何时、哪些会话、多少 token / 成本 / 延迟，全程可查；
4. 兼容存量：默认行为不破坏现有单实例部署。

## 二、已落地的隔离边界

| 资源 | 方案 | 落点 |
|---|---|---|
| 会话与消息 | `messages` 表加 `user_id/channel`；session_id 服务端命名空间 `web:{uid}:{id}` | `storage.py` / `server.py` |
| 历史接口越权 | `/api/agent/sessions*`、`/api/sessions/*` owner/admin 双校验 | `server.py` |
| 记忆 | DB `memories(scope, owner_id)` + 注入按 `ctx.user_id` 过滤 | 既有机制确认生效 |
| 工具/子代理 RBAC | executor `check_tool/check_agent` 按 role | 既有机制确认生效 |
| token/性能 | `usage_records` 落 `duration_ms/cache`；P50/P95 聚合 | `storage.py` / `usage.py` |
| 个人用量 | `GET /api/usage`（self 范围） | `server.py` / `UsageView.vue` |
| 管理端大盘 | `/api/admin/*`（admin 专属） | `server.py` / `MonitorView.vue` |
| 服务端共享文件列表 | `/api/workspace/files` 收敛为 admin 可见 | `server.py` |

## 三、新增：Web 多用户 Worker 池（核心）

文件：`src/web/worker_pool.py`

问题：所有 web 用户若共用 root Agent 实例，会共享其 tracer/实例状态与
`workspace/`（文件互相可见），实例级上下文在并发会话间串扰。

方案：为每个用户分配独立 Agent worker：

```
HTTP(员工A) ──▶ worker[u_1](workspace=ws/users/u_1, 独立 tracer/session/subagent)
HTTP(员工B) ──▶ worker[u_2](workspace=ws/users/u_2, ...)
```

- `worker.parent_agent = root`：继承 root 的 storage、LLM client、插件管理器；
  `worker.persist_session = True` 保证会话跨轮次续聊不清空（core.run 据此跳过清空分支）。
- `worker.workspace = <root workspace>/users/u_{uid}`：文件工具、task_dir、报告全部按人隔离，
  与记忆隔离、消息隔离形成“三隔离”。
- worker 拥有独立的 `session_manager / subagent_manager / tracer / hooks` → 用户间零实例串扰。
- worker 被回收后，再次承接旧会话由 `core.run._restore_db_session_history()` 从 DB 恢复上下文。

### 启用方式（默认关闭，兼容存量）

```bash
AGENT_WEB_POOL_SIZE=16  # >0 时启用；建议 4~32，按内存评估
```

### 容量与回收

- 达到上限时回收最久未用的空闲 worker；全部忙碌时临时扩容并告警；
- 空闲超过 `idle_ttl`（默认 1h）后台清理，释放 MCP/temp/会话；
- 管理端 `/api/admin/stats` 返回 `pool` 现场（容量/活跃/忙碌/已建/用户列表）。

## 四、会话连续性补强（进程重启 / worker 回收）

原有缺口：进程重启后 AgentSession 内存清空，续聊上下文丢失（只有 UI 层能看到 DB 历史）。
新增 `_restore_db_session_history(session)`：

- 触发条件：会话“仅含 system 消息”且 DB 中确有该 session 历史；
- 行为：按原顺序回填 user/assistant/tool（含 tool_calls 配对），不重复落盘；
- 收益：CLI/钉钉/web 任意的重启、换进程、worker 回收后，都能无损续聊。

## 五、管理可观测（会话 / token / 性能）

- `GET /api/admin/stats`：实时会话、运行中流、在线用户、pool、今日 token/成本、P50/P95；
- `GET /api/admin/usage?days=&group=`：按 天/用户/模型/会话 聚合；
- `GET /api/admin/sessions` + `/{id}/messages`(GET) / `(POST 导出)`：审计列表/明细/导出；
- `GET /api/usage`：员工本人 token/成本（个人用量页）。

## 六、灰度与回滚

- Worker 池通过环境变量开关，默认 0（仍走 root 单实例，行为与旧版一致）；
- DB 迁移全部幂等（`ALTER TABLE ... IF NOT EXISTS` 语义由 try/except 保证），
  升级自动完成，无需手工步骤；
- 存量历史数据：`messages` 的 `user_id/channel` 启动时自动回填一次。

## 七、会话模型：对话优先(Conversation-first)

原实现里“会话”被挂在每个 Agent 实例上(main + 各子代理各自一套 session)，同一次用户对话
被拆成多个碎片、且混入无主的随机子会话。重构为一等实体 **conversation**：

- `messages.conversation_id`：一个用户对话。顶层 run 的 `session_id` 即对话根
  (`web:{uid}:{rand}`)；**子代理/团队成员运行继承同一 conversation_id**
  （`RunContext.conversation_id` 由 `core.py` 顶层下发）。
- 内部上下文线程：`session_id = <对话根>#<agent>`（确定性命名，同一对话内复用保持连续，
  不同对话/用户不再互相串用 —— `SubagentInstance.conversation_id` + 对话级模板复用判定）。
- 展示/审计按对话聚合：`storage.list_conversations()`(主对话条数 + `thread_count`)；
  普通用户/管理端历史列表都不再出现内部碎片；admin 可用
  `/api/admin/sessions/{conv}/threads` 下钻溯源。
- 老数据迁移：无 `conversation_id` 的行以自身 `session_id` 兜底为对话根（幂等，启动自动执行）。

## 八、遗留（非本次范围）

- 若启用 Worker 池，插件（钉钉/飞书）仍走 root 实例，多用户插件渠道隔离需插件层按
  user 维度收敛（P3）；
- 用户级临时报告统一写入各自 workspace，跨用户共享知识仍由 global 记忆审批流程控制；
- 进程级横向扩容（多 agent 实例 + 共享 storage）未做，仍为单进程多 worker。
