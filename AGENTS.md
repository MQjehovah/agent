# AGENTS.md

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env — OPENAI_API_KEY is required

# Run the agent (interactive mode)
python src/main.py

# Run with options
python src/main.py --debug              # Enable DEBUG logging
python src/main.py --no-plugins          # Skip plugin loading
python src/main.py --no-scheduler       # Skip scheduled tasks
python src/main.py --workspace ./ws     # Agent working directory (default: ./workspace)
python src/main.py --config ./cfg       # Config directory (default: ./config)

# Web 多用户在线模式(公司共用)
python src/main.py --web                # Web UI on :8080(默认单实例)
AGENT_WEB_POOL_SIZE=16 python src/main.py --web   # >0: 启用按用户隔离的 Worker 池
# 可选: 容量/溢出(默认=容量)、获取等待秒数、也可写 config.json web.pool_size
AGENT_WEB_POOL_OVERFLOW=0 AGENT_WEB_POOL_ACQUIRE_TIMEOUT=15 python src/main.py --web
# 对话级限流(每用户): 并发进行中会话数 / 每分钟消息数(写 config.json web.* 亦可)
AGENT_WEB_MAX_CONCURRENT_STREAMS=2 AGENT_WEB_RATELIMIT_PER_MIN=20 python src/main.py --web
# 危险操作确认(审批联动): 1=写操作经 SSE ask 由员工确认(admin 豁免)
AGENT_WEB_CONFIRM=1 python src/main.py --web
# Web 登录会话(agent JWT)有效期(秒); 默认 43200(12h), 非正数/非法值回退默认
AGENT_SESSION_TTL_SECONDS=43200 python src/main.py --web
# 健康检查/指标: GET /healthz /metrics(内网/监控用, 不鉴权)
```

## Lint & Test

```bash
# Lint (required before commits)
ruff check src/ tests/

# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=src --cov-report=xml

# Run a single test file
pytest tests/unit/test_tools.py -v
```

CI runs: `ruff check src/ tests/` → `pytest tests/ -v --cov=src` → Docker build. Lint must pass before tests run.

## Architecture

- **Entry point**: `src/main.py` — `asyncio.run(main())`, sets up Agent, PluginManager, SchedulerManager, then enters interactive REPL loop
- **Agent core**: `src/agent.py` — `Agent` class, tool-call loop with `max_iterations=100`
- **LLM client**: `src/llm.py` — `LLMClient` wrapping `AsyncOpenAI`, handles retry/streaming/usage tracking
- **Prompt builder**: `src/prompt.py` — `PromptBuilder` assembles system prompt in static + dynamic sections (static section is cacheable)
- **Session management**: `src/agent/session.py` — `AgentSession` dataclass, message history with TTL-based expiry
- **会话续聊恢复**: `Agent._restore_db_session_history()` — 进程重启 / worker 回收后首次承接旧会话时从 `messages` 表按序回填上下文(不重复落盘)
- **RunDispatcher**: `src/agent/runner.py` — 把 `Agent.run()` 的 团队/reflective/ReAct 三路选择收敛为单一分发入口(`agent/core.py` 不再堆叠形态 if/else)
- **Web 多用户 Worker 池**: `src/web/worker_pool.py` — `AGENT_WEB_POOL_SIZE>0` 时每个用户独立 Agent worker(`workspace/users/u_{uid}` + 独立 tracer/session/subagent), 隔离用户间实例/文件/记忆上下文; LRU 容量回收 + 空闲 TTL 清理
- **管理可观测 API**(admin): `/api/admin/stats|usage|sessions|sessions/running|sessions/{id}/messages`(查看/导出)、个人用量 `/api/usage`、个人工作台 `/api/my/overview`(总览卡片: 本人跨渠道会话/运行中/记忆统计, 与角色无关)
- **「运行中」会话**: `GET /api/agent/sessions/running`(本人)、`GET /api/admin/sessions/running`(admin 全量含姓名)。口径: 正在执行(占用 Agent worker / 流式中), 与 metrics `agent_running_streams` 同源; worker 池启用时以池登记 `WebUserWorkerPool.register_run/running_sessions` 为准(chat/chat_stream acquire 后登记、release 前注销), 池关闭时回退内存 `ChatSession.is_streaming`。**跨渠道**: 钉钉/飞书/webhook/定时等非 web 执行经 `MessageRouter.route` 前后写入进程级登记 `src/channels/run_registry.py`(除 cli 外全渠道, 与池登记/内存流式按 session_id 去重合并; 钉钉等跑在 root agent、`worker=false`, 不占池 worker)。每条含 conversation_id/channel/user/started_at/model/stage(`ChatSession.stage`, 流式 hook 事件更新工具/子代理阶段)。归属 tag 统一解析为 `{channel}:{uid}`(`WebServer._tag_uid`), 跨渠道同 agent 用户可见
- **Sub-agents**: `src/subagent_manager.py` — loads sub-agent templates from `config/agents/*/PROMPT.md`, reuses sessions by name
- **Memory**: `src/memory/manager.py` — DB 记忆按 `owner_id` 隔离(user 私有 + global 公共)
- **Learning**: `src/learning/learner.py` — self-learning module that triggers pattern extraction and skill creation
- **Storage**: `src/storage/storage.py` — unified SQLite with connection pool; single `config/data.db`; singleton via `init_storage(workspace, config_dir)`. 表: `messages`(含 `user_id/channel/conversation_id` 审计列)、`eventbus_events`、`autonomous_goals`、`kanban_tasks`、`scheduled_tasks`、`rbac_roles`(含 `permissions/data_scope`)/`rbac_users`/`rbac_user_identities`/`rbac_departments`、`memories/memory_proposals`、`web_tokens`、`usage_records`(含 `duration_ms/cache_*`, 聚合 `summarize_usage/usage_totals`)、`session_meta`、`webhook_tasks`(webhook 任务持久化+重启续跑)、`mcp_calls`(MCP 调用审计: server/tool/耗时/成败/归属, `record_mcp_call`/`query_mcp_calls`/`prune_mcp_calls`(启动清理 >30 天))。启动自动做幂等 `ALTER` 迁移
- **Plugins**: `src/plugins/` — `BasePlugin` ABC; plugins loaded from `src/plugins/` dir, provide extra tools to agents
- **钉钉工具确认回路**(互动卡片, fail-closed): 纯逻辑在 `src/plugins/dingtalk/confirm.py`(`create_card_confirmer`/`CardActionRegistry`/payload 构建/超时解析); 钉钉每次 `router.route` 前后由 `confirm_scope` 装配——AUTO 模式临时降 DEFAULT, `agent.on_confirm` 按当前 run `conversation_id` 派发(并发 run 隔离、非钉钉 run 回退原回调); 写操作向触发人**私聊**发互动卡片(通用 AI 卡片模板, `callbackType=STREAM`), 按钮回调 topic `/v1.0/card/instances/callback`; 超时(默认 120s, `DINGTALK_CONFIRM_TIMEOUT` 覆盖)/发送失败/回调主题不可用一律拒绝, 渠道不可用时最终回复固定文案; 同一 request_id 首个裁决生效(重复点击幂等)
- **MCP dingtalk 办公 API**: `mcp_server/src/dingtalk.py` 除消息/通讯录/卡片外, 新增审批(`dingtalk_approval_start/instance/tasks/action`)、待办(`dingtalk_todo_create/update/list`)、日程(`dingtalk_calendar_create_event/list_events/freebusy`); 注解: 查询 readOnly、创建/更新非破坏写、审批同意/拒绝 destructive; 统一 `_api` + 响应截断防超长
- **MCP servers**: `src/mcps/manager.py` — launches external MCP tool servers defined in `config/mcp_servers.json`; 每 server 可选治理字段(缺省保持旧行为): `timeout_seconds`(工具调用超时, 默认60; remote_terminal 生产配 300)、`connect_timeout_seconds`(默认30)、`max_reconnect_attempts`(默认3)、`max_concurrency`(默认4, 信号量排队)、`risk_overrides`(原始工具名→`read|write|destructive`, 覆盖工具注解); 工具级风险由 `annotations`(readOnlyHint/destructiveHint) 映射(read/write/destructive/unknown), 经 `MCPManager.tool_risk/tool_server/tool_raw` 暴露: `PermissionChecker` 按模式处理(PLAN 拒写类、SMART 仅 destructive 需确认/write 放行、DEFAULT destructive/write 需确认、AUTO 放行; 注: PLAN 对无注解 `unknown` 的 MCP 工具不拦截, 不构成只读保障, 需靠白名单/风控兜底), destructive MCP 工具结果命中敏感改道; 自研 server 工具已批量补注解(`mcp_server/src/*.py`, 写操作 destructive、读 readOnly、消息发送/配置类非破坏写); 每次 MCP 调用(成功/失败)落 `mcp_calls`(耗时/结果大小/归属), 状态经 `MCPManager.status_all()` 聚合(各实例状态/失败清单/重连次数), `/healthz` 带 `mcp` 字段(不鉴权), `GET /api/admin/mcp` 返回完整状态、`GET /api/admin/mcp/calls?limit=&server=&tool=` 查询审计(均需 `admin.monitor`, limit ≤1000)
- **平台 MCP 轨(市场能力)**: `src/mcps/platform.py` — `MARKET_BASE_URL`+`MARKET_SERVICE_TOKEN` 齐备才启用(缺失=关闭, 仅本地 MCP); `PlatformMCPClient` 拉 `GET {MARKET_BASE_URL}/api/capabilities/sync`(Bearer 服务令牌)筛 `type=mcp` 且 `distribution in (remote,both)`(local 网关 403 直接跳过), 经能力网关 `/api/mcp-gateway/relay/{name}/stream`(Streamable HTTP, `name@version` 钉版本; 有 `gateway.stream_url` 时优先)用 MCP SDK 连接并 `list_tools`; 工具暴露名 `platform__{能力}__{工具}`(本地/保留名优先, 平台同名跳过), 风险映射与本地同源(注解→read/write/destructive/unknown); `MCPManager.attach_platform` 组合后 LLM 工具表、`PermissionChecker` risk_resolver、`mcp_calls` 审计(server=`platform:{能力}`, 带 conversation/user 归属)、`/healthz` 与 `/api/admin/mcp`(独立 `platform` 块, 每能力行 `source=platform` 带 version/last_refresh)全覆盖; 根 agent 与 worker 池用户 worker 挂接(子代理不重复建连), 周期刷新(`MARKET_PLATFORM_REFRESH_SECONDS` 默认 300, `MARKET_PLATFORM_TIMEOUT` 默认 60, 非法回退)随 Agent 初始化启动、cleanup 取消; sync 失败仅告警保留既有连接, 单能力失败隔离(连接分小批 ≤2、批间隔 0.3s 推进, 避免瞬时并发拉起上游; 失败项 30s 起指数退避快速重试、封顶刷新周期、成功清退避计数), 版本变化重连; last_error 经 `unwrap_error` 解包 ExceptionGroup 展示根因; 令牌不落日志/状态
- **Commands**: `src/cmd_handler.py` — `/` commands in interactive mode (e.g. `/help`, `/agents`)

## Directory Layout

### Config directory (`--config`, default: `config/`)

Contains all configuration and runtime state (mounted in Docker):

```
config/
├── PROMPT.md              # Root agent system prompt (frontmatter: name, description)
├── agents/                # Sub-agent definitions (each dir has PROMPT.md)
│   ├── 设备运维/
│   ├── 数字中台/
│   ├── 售后客服/
│   ├── 代码审查/
│   ├── IT运维/
│   └── AI开发团队/        # Team agent with skills + references + sub-agents
│       ├── skills/        # 23 shared lifecycle skills (each may have references/)
│       └── agents/        # 7 sub-agent personas
├── skills/                # Skill definitions (each has SKILL.md)
│   └── report-writer/
├── memory/                # Auto-managed (gitignored)
├── data.db                # Unified SQLite storage (gitignored) — messages, events, goals, kanban
├── mcp_servers.json       # MCP server configs
├── schedules.json         # Cron-based scheduled tasks
├── dingtalk.json           # DingTalk plugin config
└── webhook.json            # Webhook plugin config
```

### Agent workspace (`--workspace`, default: `workspace/`)

Agent working directory where file operations, shell commands, and artifacts are created:

```
workspace/                # Auto-created, gitignored
└── users/                # Worker 池启用后按用户隔离: users/u_{uid}/(各用户独立工作区)
```

> 非 worker 模式(默认)所有渠道共享 `workspace/`, 该服务端共享目录仅 admin 可见(`GET /api/workspace/files`)。

## Key Conventions

- **All source is under `src/`** — there is no package namespace; modules import each other directly (e.g. `from agent import Agent`)
- **Tests add `src/` to `sys.path`** manually (`sys.path.insert(0, ...)`) — no `pyproject.toml` package install
- **Language**: Code comments, log messages, and workspace content are in Chinese; variable names and docstrings are English
- **Environment**: `.env` loaded via `python-dotenv` at startup; falls back to `.env.example` if `.env` missing
- **报告生成约定**: 默认不生成报告文件，结果直接在对话中输出；仅当用户明确要求“生成/保存报告文件”时才生成，且统一写入工作目录下的 `.agent/report/` 目录
- **Workspace PROMPT.md** uses frontmatter (`---\nname: ...\ndescription: ...\n---`) parsed by `utils/frontmatter.py`
- **Permission modes**: `default` (confirm writes), `smart` (仅 destructive 需确认, write 放行), `auto` (allow all, for containers), `plan` (read-only) — set in `Agent.__init__`. 注: `plan` 只拦显式 write/destructive 注解的 MCP 工具, 对无注解(unknown)工具不构成只读保障
- **Logging**: Uses `rich.logging.RichHandler` with aligned logger names; API calls logged to `logs/api_YYYYMMDD.log`
- **Sandbox**: Optional sandbox via `config/sandbox.json` (process or Docker mode). Intercepted at `Agent._sandbox_intercept()` — tools remain unaware of sandboxing
- **Team pipeline**: `TeamOrchestrator` supports `default`/`feedback`/`auto` modes. `feedback` mode enables dev↔test feedback loops with automatic retry. `auto` mode uses LLM to dynamically generate pipeline stages

## 多用户隔离与审计(公司级在线 Agent)

- **对话优先(Conversation-first)**: `messages.conversation_id` 定义“一个用户对话”。顶层 run 的 `session_id` 即对话根(`web:{uid}:{rand}`); 子代理/团队成员运行继承同一 `conversation_id`, 其内部上下文为确定性线程 `session_id = <对话>#<agent>`(`src/agent/core.py` RunContext 下传)
- **列表/续聊/审计按对话聚合**: `/api/agent/sessions/history`、`/api/admin/sessions` 走 `storage.list_conversations()`(主对话条数 + thread_count), 不再按 agent 碎片列出; 审计可经 `/api/admin/sessions/{conv}/threads` 下钻内部线程。普通用户「我的会话」= 同一 agent 用户跨 web/钉钉/其它 `tag:{uid}` 渠道合并(`storage.list_conversations_for_agent_user()`), 每条带 `channel` 字段(供前端渠道徽标; 个人「对话」页侧栏即此跨渠道历史, 钉钉等外部会话点击**只读**查看)。**个人口径约定**: `/api/sessions`、`/api/agent/sessions/history`、`/api/memories`、`/api/scheduler/tasks` 默认个人口径(admin 亦只看自己): `/api/memories` mine=仅本人私有(user scope, **不含 global 公共**), `/api/scheduler/tasks` mine=仅本人创建的 DB 任务(排除 static 系统级与他人); 仅当 admin 显式传 `scope=all`(`/api/memories` 为 `view=all`) 才返回全站(定时含 static、记忆含 global+全部人私有), 供「运行监控」运维组使用。**续聊写回仅允许 web 前缀会话**: `/api/chat` 与 `/api/chat/stream` 对非 web `session_id` 返回 400(钉钉等外部渠道历史只读, 由对应渠道插件续聊, 不经过 /api/chat); 属主读取任意渠道消息(含 DB 回退)不受限
- **子代理按(对话, agent)隔离**: `SubagentInstance.conversation_id` + `_name_to_session` 仅在相同对话内复用, 杜绝跨对话/跨用户串上下文; 老随机子会话由启动迁移以自身 session 兜底为对话根
- **会话命名空间**: web 渠道 session_id 服务端强制 `web:{uid}:{rand}`; 非属主访问一律 404
- **审计落盘**: `messages` 表带 `user_id/channel/conversation_id` 列; 会话内容全部落库,admin 可经 `/api/admin/sessions/{id}/messages` 查看/导出
- **用量/性能**: LLM 调用写入 `usage_records`(含 `duration_ms`, 供 P50/P95); 个人用 `GET /api/usage`,管理端用 `/api/admin/usage`
- **内存/文件隔离**: 记忆按 `owner_id` DB 隔离; worker 池启用后文件按 `workspace/users/u_{uid}` 隔离
- **角色 + 部门权限控制**(2026-09-15 强化): `rbac_roles` 新增 `permissions`(Web 管理权限键, `*` 通配) 与 `data_scope`(`all` 全站 / `department` 本部门 / `self` 仅本人); 内置 `admin` 恒 `["*"]`+`all`、`default` 恒 `[]`+`self`(按角色名特殊处理, 老库升级安全)。`allowed_tools` 支持只读限定条目 `{tool}:read`(如 `file:read`/`shell:read`), 由 `PermissionChecker.classify_access`(file 读操作/shell 读前缀/MCP read 注解 → read, 其余保守按 write)判定后拒绝写调用; 旧条目 `{tool}` 仍读写全放行。权限键目录见 `security/rbac.py:WEB_PERMISSIONS`(admin.users/roles/departments/monitor/logs/memories/scheduler/workspace)。鉴权统一走 `web/security.py`(`get_authz`/`require_perm`/`has_permission`/`scope_department`), 主服务 `_require_perm`、Router 用 `perm_or_403`; `admin.*` 系列原「凭 role==admin」判定改为权限判定, 并补齐此前**无守卫**的 RBAC 端点(roles/users GET·DELETE·identities)。数据隔离: `data_scope=department` 的管理员仅可见/可管本部门成员(用户列表、管理端会话/用量/运行中/定时任务全量视图均按 `rbac_users.department` 过滤; 存储层 `query_usage/summarize_usage/usage_totals` 支持 `uids` 过滤), 且不得分配超管/全站范围角色、不得跨部门调整成员。`rbac_departments` 表 + `/api/rbac/departments` CRUD(有成员时禁删), 部门改名同步成员归属。前端 `/api/auth/me`·login 回包带 `permissions`/`data_scope`/`department`; 菜单/路由按 `hasPerm(key)` 收敛(`main.ts` `perm`/`permAny`), 「用户与权限」页含用户/角色/部门三个 Tab(按权限显示)
- **Worker 池**: `AGENT_WEB_POOL_SIZE>0` 启用(每用户独立 Agent), 容量守护: 溢出上限
  `AGENT_WEB_POOL_OVERFLOW`(默认=容量, 硬顶2x; 0=不许溢出), 饱和时短暂等待
  `AGENT_WEB_POOL_ACQUIRE_TIMEOUT` 后返回 503(不再无界扩容); 详见 `docs/refactor-blueprint.md`
- **文档同步**: 改动代码需同步维护本文件与 `docs/`、`frontend/README.md`

## Skill Lifecycle — Automatic Routing

The agent uses the `skill` tool to load structured workflows. Skills follow the lifecycle: **DEFINE → PLAN → BUILD → VERIFY → REVIEW → SHIP**. Before ANY action, check skill applicability:

### Intent-to-Skill Routing (always check first)

| User says / Task type | Load this skill first | Followed by |
|---|---|---|
| "build a feature", new project, new feature | `spec-driven-development` | plan → build → test → review |
| vague idea, unclear requirements | `interview-me` | spec-driven-development |
| "plan this", "break this down" | `planning-and-task-breakdown` | — |
| implement, code, write code | `incremental-implementation` + `test-driven-development` | review |
| fix a bug, debug, something broke | `debugging-and-error-recovery` | tdd (regression test) |
| review this, code review, check my code | `code-review-and-quality` | — |
| security, audit, is this secure | `security-and-hardening` | — |
| performance, slow, optimize | `performance-optimization` | — |
| deploy, release, ship, publish | `shipping-and-launch` | — |
| git, commit, push, branch | `git-workflow-and-versioning` | — |
| design API, interface, module boundary | `api-and-interface-design` | — |
| document, ADR, changelog | `documentation-and-adrs` | — |
| CI/CD, pipeline, build, deploy pipeline | `ci-cd-and-automation` | — |

### Skill Activation Rules

1. **Always check** if a skill applies before acting. The `using-agent-skills` meta-skill can help route.
2. **If a skill applies, use it.** Don't skip required workflows (spec, plan, test, review).
3. **Follow the skill's process exactly** — steps, rationalizations table, red flags, verification checklist.
4. **Verification is non-negotiable.** Every skill ends with evidence requirements. "Seems right" is never sufficient.
5. **Anti-rationalization.** If you think "I can skip this step", read the Common Rationalizations table in the skill first.
6. Red flags in a skill mean you're violating it. Stop and correct course.

### Reference Checklists

Quick-reference materials are in individual skill directories under `skills/<skill>/references/`:
- `code-review-and-quality/references/definition-of-done.md` — Project-wide standing bar
- `test-driven-development/references/testing-patterns.md` — Test structure, naming, mocking
- `security-and-hardening/references/security-checklist.md` — Pre-commit security checks
- `performance-optimization/references/performance-checklist.md` — Core Web Vitals targets
- `frontend-ui-engineering/references/accessibility-checklist.md` — WCAG 2.1 AA checks
- `observability-and-instrumentation/references/observability-checklist.md` — RED metrics, logging, alerting

## Docker

```bash
docker build -t agent .
docker run --rm -e OPENAI_API_KEY=sk-... agent
```

Port 8081 is exposed (for plugins/webhook). Default CMD runs `python src/main.py --debug`.

员工端前端(`frontend/`)由镜像**阶段 1** 用 node 构建, 产物 `src/web/static_vue/` **不入库**(`.gitignore`/`.dockerignore` 均忽略; `.dockerignore` 只排除 `frontend/node_modules`)。本地开发需先 `cd frontend && npm run build`, 否则 `/` 回落旧版单页控制台(`static/`)。

## Pitfalls

- **`.env` is gitignored** — never commit API keys. Use `.env.example` as template.
- **钉钉凭证走环境变量** — `config/plugins/dingtalk.json` 已移出版本库(模板 `dingtalk.example.json`)；`DINGTALK_APP_KEY`/`DINGTALK_APP_SECRET`(与 `mcp_server/src/dingtalk.py` 同名)优先于配置文件，`config/**/mcp_servers.json` 内不再存 AppSecret，MCP 子进程经进程环境继承
- **飞书凭证走环境变量** — `config/plugins/feishu.json` 已移出版本库(模板 `feishu.example.json`)；`FEISHU_APP_ID`/`FEISHU_APP_SECRET` 优先于配置文件
- **APP_ENV 分级守卫** — `src/utils/env_guard.py` 统一约定: `APP_ENV=production/prod`(大小写不敏感, 默认 `development`)下 `JWT_SECRET`/`AGENT_ADMIN_PASSWORD` 缺失或命中弱值(如 `admin123`/`change-me`/`xzyz2022!`)将拒绝启动(错误含变量名); 开发放行并打印 WARNING
- **首次 admin 口令不写死** — `_ensure_admin_user` 读 `AGENT_ADMIN_PASSWORD`: 生产未配置即启动失败; 开发未配置时随机生成一次性口令并**仅打印一次**(日志中不再回显固定口令)
- **JWT 密钥不落版本库** — `config/jwt_secret` 已移出版本库并 gitignore(模板无需, 首启自动生成); 生产必须注入 `JWT_SECRET`, 开发未配置时回退读取该文件
- **MCP 数据库/终端口令走环境变量** — `mcp_server/src/mysql_query.py` 读 `DB_PASSWORD`、`remote_terminal.py` 读 `TERM_PASSWORD`, 经 `mcp_server/src/env_guard.py` 复用 `src/utils/env_guard.py` 的生产拒绝/开发告警语义, 代码内不再内置真实口令
- **MCP 凭证走环境变量** — `config/mcp_servers.json` 与 `config/agents/*/mcp_servers.json` 中的 `SMTP_PASSWORD`/`DEVICE_API_PASSWORD` 已改为 `${SMTP_PASSWORD}`/`${DEVICE_API_PASSWORD}` 占位符; `src/mcps/manager.py` 启动 MCP 子进程时从进程环境解析占位符并合并父进程 env(未定义则不覆盖, 由子进程继承), 真实值放本机 gitignored `.env`
- **平台 MCP 轨默认关闭, 启用需重建镜像** — `MARKET_BASE_URL`/`MARKET_SERVICE_TOKEN` 写 `.env`(模板见 `.env.example`), 两者齐备才启用; `build.sh` 未传 `--env-file`, `.env` 由 Dockerfile `COPY . .` 打进镜像, 改配置后须 `docker build` 重建容器; 服务令牌绑定的市场账号需有运行时准入(生产用 admin 绑定), 能力 `distribution=local` 会被网关 403 并跳过
- **`config/memory/` and `config/sessions/` are gitignored** — they contain runtime state
- **`docs/plans/` is gitignored** — design docs live there but are not tracked
- **Sub-agent names are Chinese** (e.g. `设备运维`) — this is intentional, not a mistake
- **`OPENAI_BASE_URL` defaults to Alibaba DashScope**, not `api.openai.com` — change in `.env` if using a different provider
- **`max_retries=0` on OpenAI client** — all retries are handled by our application-level retry logic in `LLMClient`, not by the httpx SDK
- **LLM timeout is configurable**: `LLM_TIMEOUT` (default 300s, read timeout) and `LLM_CONNECT_TIMEOUT` (default 30s, connection timeout)
- **MCP SDK v2(Python)** — 已迁 `mcp>=2.2,<3`(客户端与自研 server 同步)：`FastMCP`→`MCPServer`(`from mcp.server.mcpserver import MCPServer`)、协议字段 snake_case(`input_schema/is_error/structured_content`)、**同步 handler 跑 anyio worker 线程**(涉及事件循环或共享可变状态的工具必须 `async def` 或加锁)、SDK 网络改用 `httpx2`(日志记录器 `httpx2`/`httpcore2`)、`nest_asyncio` 已移除、**stdio 关闭语义**: 先关 stdin 等优雅退出再升级杀进程树(POSIX 优雅退出不杀孙进程, 超时对新进程组 SIGTERM→SIGKILL; Windows Job Object 直接终止进程树), **客户端连接/收尾走专属连接任务**(anyio cancel scope 进出同任务, 关闭 shielded 等待不被取消打断)
- **MCP servers 默认启用状态不一** — `config/mcp_servers.json` 中 `time`/`fetch` 默认 `"enabled": true`(纯离线/只读)；`filesystem`/`git`/`postgres`/`mysql_query` 默认 `"enabled": false`，需显式开启并配置白名单或连接串(`FS_MCP_ROOTS`/`GIT_MCP_ROOTS`/`PG_MCP_DSN` 等)；`filesystem`/`git` 写操作另需 `*_ALLOW_WRITE=true`。W3 新增内置 server: `time`(时区)/`fetch`(抓取, SSRF+CGNAT 防护)/`filesystem`(受限目录, symlink 不跟随)/`git`(仓库白名单+gitdir 越界防护)/`postgres`(只读事务+行数/超时上限)，详见 `mcp_server/README.md` 与配置内中文描述
- **`AGENT_WEB_POOL_SIZE` 默认 0** — 多用户部署需显式设 >0; 启用后每个用户首次请求会触发 worker 冷初始化; 回滚/单实例直接置 0
- **DB 迁移幂等自愈** — `messages.user_id/channel`、`usage_records.duration_ms/cache_*`、`rbac_users.display_name` 由启动时 `ALTER` 自动补齐并回填一次历史(web:{uid}); 无需手工
- **SSO 账号模型** — `rbac_users.name` 恒为工号(sub, 唯一身份键), 中文显示名放 `display_name`; `_sso_ensure_user` 未按工号命中时会按 `claims.name`(中文) 找老账号并把其 name 改成工号(双账号自动合并, 保留 role/dept/status/钉钉绑定/历史); `claims.dingtalk` 登录成功后自动 `bind_identity`(幂等), 钉钉渠道免人工开号。展示类接口(name/owner)同时返回 `display_name` 字段, 前端优先用, 内部归属仍按 name=工号/tag
- **SSO 双轨配置** — 资源轨(校验别人传来的 token): `SSO_ISSUER`/`SSO_AUDIENCE`/`SSO_JWKS_URI`; 登录轨(浏览器授权码流程 `/api/auth/sso/start` + `/api/auth/sso/callback`): 追加 `SSO_CLIENT_ID`/`SSO_CLIENT_SECRET`/`SSO_REDIRECT_URI`/`SSO_REDIRECT_TARGET`(子路径+hash 路由部署时为 `/agent/#/login`)。`is_configured()` 只看 issuer, 故登录轨配不全时 `/sso/start` 会 302 但换 token 必失败 — 两轨要一起配。容器内解析不到公网域名时需 `--add-host auth.xzrobot.com:192.168.31.45`(已固化在 `build.sh`), 否则回调在 `issuer/token` 一步失败
- **`X-Service-Token` 等同服务账号** — `_is_service_request` 命中的请求在 `_get_admin` 与 `_get_authz` 都按 `{uid:0, role:admin}` 处理(细粒度端点不再 401), 故该令牌权限等同管理员, 只下发给可信服务(网关/工作台); `/api/chat` 与 `/api/chat/stream` 未鉴权一律 401(走 `_get_authz`, 不再回落 `web:anon`; `WEBUI_DISABLE_AUTH=1` 仍由 `_get_auth` 处理)
- **测试勿在模块导入时改环境** — `WEBUI_DISABLE_AUTH` 等必须用 autouse fixture(monkeypatch)注入; 导入期设置会泄漏给后续测试文件, 曾使 `test_sso_auth` 双轨鉴权用例假失败
- **会话隔离依赖命名空间** — 非 web 前缀的旧会话无法回填归属, 对非 admin 普通用户不可见(安全优先), admin 仍可审计; 改造前旧格式钉钉会话(dingtalk:{staff_id})同理保持不可见
- **跨渠道合并仅限同一 agent 用户** — 钉钉改造后归属 tag 为 `dingtalk:{agent_user.id}`(与 web:{uid} 同 rbac 用户), web「会话」页才会合并展示并带渠道徽标; 钉钉会话命名/复用(2026-09-08 群模型 Phase1): 单聊根 `dingtalk:{uid}:{rand}`、群共享根 `dingtalk_group:{safe}:{hash}:{rand}`(前缀即类型)。同 scope(单聊=agent_uid / 群聊=cid 规范前缀) 复用「最近单聊根」或 `dingtalk_scopes` 持久指针, **跨进程重启可续根**(指针/最近根落库); `/new` 开新根并覆写指针。群共享根整群一个上下文、群间并发/群内串行(现有 `_conv_lock`), 群 run 不注入触发人私有记忆(记忆工具群内置空属主只读 global), 群根历史参与者(根下出现过 `dingtalk:{uid}` 的成员)+admin 可见; 恢复上下文能力仍按 `_restore_db_session_history`。**Phase2(敏感结果私聊送达)**: 敏感工具清单默认 = mysql_query MCP 三工具 `list_tables`/`describe_table`/`execute_query`(config.json `sensitive.tools` 可覆盖, 读取集中 `agent/sensitive.py`); 工具执行层命中清单 → 给当前 run 打 `sensitive_hit` 标记(RunContext/AgentResult, 子代理调用链经 `agent/core.py` run 结束时汇聚到顶层); 钉钉群共享会话(`dingtalk_group:`)且本轮命中敏感 → 最终回复文本不落群, 经 oToMessages 私聊送达触发人(staff_id), 群内仅发 `GROUP_SENSITIVE_PRIVATE_NOTICE` 占位; 单聊与非敏感工具群内照常直接回复。**Phase2 收尾(敏感内容不落群根)**: 群共享根每一轮 run 的落库消息带整轮标记 `messages.round_id`(顶层群 run 用自身 run_id, 子代理线程继承); 命中敏感的那一轮在 `agent/core.py` run 收尾时经 `storage.relocate_round_to_dingtalk_private` 把 `conversation_id=群根 且 round_id=该轮` 的整轮(敏感工具产出 + 含敏感结果的最终回复 + 该轮触发问题)从群根 `messages` 搬到私有旁路表 `dingtalk_private_messages`(按 `dingtalk:{uid}` owner_tag 关联, 触发人本人可查、他人/群根历史不可见、上下文恢复不含该轮), 并把该轮从群共享内存上下文回滚; 非敏感群轮/单聊照常实时落库不受影响。触发人读取路径: `GET /api/my/dingtalk/private-rounds`(个人口径, admin 亦只看自己) + `storage.list_dingtalk_private_rounds/messages`