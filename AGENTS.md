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
- **Storage**: `src/storage/storage.py` — unified SQLite with connection pool; single `config/data.db`; singleton via `init_storage(workspace, config_dir)`. 表: `messages`(含 `user_id/channel/conversation_id` 审计列)、`eventbus_events`、`autonomous_goals`、`kanban_tasks`、`scheduled_tasks`、`rbac_roles/users/user_identities`、`memories/memory_proposals`、`web_tokens`、`usage_records`(含 `duration_ms/cache_*`, 聚合 `summarize_usage/usage_totals`)、`session_meta`、`webhook_tasks`(webhook 任务持久化+重启续跑)。启动自动做幂等 `ALTER` 迁移
- **Plugins**: `src/plugins/` — `BasePlugin` ABC; plugins loaded from `src/plugins/` dir, provide extra tools to agents
- **MCP servers**: `src/mcps/manager.py` — launches external MCP tool servers defined in `config/mcp_servers.json`
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
- **Permission modes**: `default` (confirm writes), `auto` (allow all, for containers), `plan` (read-only) — set in `Agent.__init__`
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

## Pitfalls

- **`.env` is gitignored** — never commit API keys. Use `.env.example` as template.
- **`config/memory/` and `config/sessions/` are gitignored** — they contain runtime state
- **`docs/plans/` is gitignored** — design docs live there but are not tracked
- **Sub-agent names are Chinese** (e.g. `设备运维`) — this is intentional, not a mistake
- **`OPENAI_BASE_URL` defaults to Alibaba DashScope**, not `api.openai.com` — change in `.env` if using a different provider
- **`max_retries=0` on OpenAI client** — all retries are handled by our application-level retry logic in `LLMClient`, not by the httpx SDK
- **LLM timeout is configurable**: `LLM_TIMEOUT` (default 300s, read timeout) and `LLM_CONNECT_TIMEOUT` (default 30s, connection timeout)
- **MCP servers in `mcp_servers.json` are disabled by default** (`"enabled": false`) — must be explicitly enabled
- **`AGENT_WEB_POOL_SIZE` 默认 0** — 多用户部署需显式设 >0; 启用后每个用户首次请求会触发 worker 冷初始化; 回滚/单实例直接置 0
- **DB 迁移幂等自愈** — `messages.user_id/channel`、`usage_records.duration_ms/cache_*`、`rbac_users.display_name` 由启动时 `ALTER` 自动补齐并回填一次历史(web:{uid}); 无需手工
- **SSO 账号模型** — `rbac_users.name` 恒为工号(sub, 唯一身份键), 中文显示名放 `display_name`; `_sso_ensure_user` 未按工号命中时会按 `claims.name`(中文) 找老账号并把其 name 改成工号(双账号自动合并, 保留 role/dept/status/钉钉绑定/历史); `claims.dingtalk` 登录成功后自动 `bind_identity`(幂等), 钉钉渠道免人工开号。展示类接口(name/owner)同时返回 `display_name` 字段, 前端优先用, 内部归属仍按 name=工号/tag
- **会话隔离依赖命名空间** — 非 web 前缀的旧会话无法回填归属, 对非 admin 普通用户不可见(安全优先), admin 仍可审计; 改造前旧格式钉钉会话(dingtalk:{staff_id})同理保持不可见
- **跨渠道合并仅限同一 agent 用户** — 钉钉改造后归属 tag 为 `dingtalk:{agent_user.id}`(与 web:{uid} 同 rbac 用户), web「会话」页才会合并展示并带渠道徽标; 钉钉会话跨进程重启不自动复用对话根(复用键在内存, 重启后新 rand), 属已知限制, 恢复上下文能力仍按 `_restore_db_session_history`