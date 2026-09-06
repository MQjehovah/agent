# Agent 改版蓝图 —— 借鉴开源，向目标架构演进

> 关联：`requirements-features.md`(F清单)、`architecture-review-2026.md`(目标架构)、
> `project-employee-ai-assistant.md`(立项)。本文给“怎么改、按什么波形”。

## 1. 借鉴的相似开源项目与可迁移点

### 1.1 Dify（LLM App 平台：多租户/运营/治理）
| Dify 做法 | 本项目的映射 | 落地 |
|---|---|---|
| 应用(agent)+会话一等资源、归属 tenant/用户 | 我们已有 conversation(对话) 一等实体 | ✅ 已收敛 |
| 配置 `.env`/环境变量分层 + 运行时集中读取 | 沿用 `settings.py`，补齐 `web.pool_size` 等键 | 本轮 |
| 运营观测：trace/logs/annotations 面板 | admin 大盘/日志流 | ✅ 已有，待指标化 |
| 用量+配额/成本归集 | `usage_records` + admin/个人用量 | ✅ 已有，配额待接 |
| 长任务经队列(Celery/Redis) 与 HTTP 解耦 | 现阶段=事件循环内任务+worker 池；远期=进程外队列 | 波形 D |
| 后台“运行时”独立于 API | =我们的 worker 池/执行层分离 | 波形 A/B |

### 1.2 OpenHands Agent Server（多 Agent 运行时 REST + 前端控制台）
| OpenHands 做法 | 本项目映射 | 落地 |
|---|---|---|
| 一个 Agent Server 跑多个 agent，conversation 一等 REST 资源 | 我们 worker 池 + conversation | ✅ 已有，继续加固容量 |
| 每个会话绑定独立 workspace/项目根 | worker 池 `workspace/users/u_{uid}` | ✅ 已实现(开池) |
| “前端控制台”与“运行后端”解耦、可换多个后端 | dashboard/Web 与 agent server | 平台侧(参考) |
| 事件流驱动(conversation 事件)推给前端 | Hook→SSE 已实现 | ✅ 已做，待加 seq/会话信封 |
| 自动化调度(automation)独立于 agent 运行 | scheduler/autonomous | 波形 C |

### 1.3 其它可借鉴（轻量）
- **Github Copilot/Codex 类**：无。
- **SearXNG/n8n**：无直接关系。
- **通用工程**：领域包结构(domain/service/adapter)、配置 schema、契约测试 —— 见 §2 波形 B。

## 2. 改版波形（Wave，按依赖与风险排序）

### Wave A · 运行时隔离与容量治理（先做）
- worker 池**溢出上限与忙碌保护**：不再无界临时扩容 → 上限内排队/等待，超限拒绝(503)并提示；stats 暴露容量/忙碌/溢出。
- `web.pool_size` 等键进配置；公司部署默认开启（ops 置 env）。
- 影响：`web/worker_pool.py`、`web/server.py`、`AGENTS.md`。
- 验收：多用户并发不崩、饱和时优雅拒绝、监控可看 pool 现场。

### Wave B · 模块化重构（域拆分 + 语义收敛）
> 进度：Step1 ✅（2026-09）——`web/security.py`(统一鉴权) + `web/routers/admin.py`
> (admin/usage/个人用量 迁出)，server.py 从 1960→~1800 行；接口不变、烟测通过。
> Step2 进行中：`web/routers/sessions.py`(会话域迁出，server.py→~1730 行)；
> Step3 ✅：`agent/runner.py` RunDispatcher 收敛 run 形态选择(core.run 不再堆 if/else)。
> 待：chat SSE 域化与 workbench 域、loop 内 react/reflective/team 去重。
- 把 `web/server.py`(1960) 拆成 FastAPI Routers：`auth`/`chat_sse`/`conversations`/
  `admin`/`usage`/`workbench`/`webhook`；共享一个轻量 `WebRuntime`(会话表/归属/pending-asks/pool)。
- 抽出领域服务：`ConversationService`(列表/明细/恢复/审计)、`RunDispatcher`(顶层/worker/子代理/团队 收敛 core.run 分支)。
- 影响：新建 `web/routers/`、`web/runtime.py`、`web/services.py`；削减 core/loop 重复。
- 验收：功能不变(烟测全绿)、server.py 瘦身、新增功能只碰一个域。

### Wave C · 交互与可靠性补齐（面向员工）
> 进度：SSE 双向 ask ✅（2026-09）——`tools/ask_user` 增加可注入追问桥，聊天流内
> 遇 ask 时发 `ask` 事件暂停，`/api/chat/answer` 续跑；前端问答栏(单选/文本/默认)。
> 待：F7 危险操作审批联动、webhook DB 化续跑、对话级限流/配额。

### Wave D · 运行解耦与运维化（远期，借用 Dify/OpenHands 形态）
- 执行入队(进程内队列→远期 Redis/worker 进程)，API 无状态化 → 多实例；
- 指标(Prometheus/Opik 式 trace)、备份/恢复制度、审计闭环、router 计量打通。

## 3. 改版完成定义(DoD)
- 每个 Wave：烟测脚本绿(登录/流式/审计/用量/隔离/新功能)、ruff 增量不回归、
  文档同步(本蓝图+AGENTS+requirements 状态)、按需部署灰度。
