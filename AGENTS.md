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
- **当前用户画像**: `Agent._apply_user_profile()`(`agent/core.py`) — 每次 run 开始时在 dynamic 段最前注入「当前用户」（姓名/工号/部门/角色/钉钉 userId，字段缺失省略），使 AI 能回答"我是谁/我属于哪个部门/我的钉钉号"；工号与显示名由 `user_id` tag 的数字 uid 查 `rbac_users` 补齐，钉钉 userId 取 `rbac_user_identities` 中 platform='dingtalk' 的 platform_uid（查不到/未绑定静默跳过）；`group_context=True`（钉钉群共享根等）不注入，沿用「群内不注入触发人私有信息」约定；与渐进披露提示共存、static 前缀与 prompt cache 不受影响；子代理继承父级字段后各自注入。解析逻辑公共件 `agent/user_profile.py`，另配只读内置工具 `whoami`（`src/tools/whoami.py`，核心工具恒注入、不进渐进检索）显式返回同一画像（群聊下仍返回触发人身份，仅身份字段）。团队/连接池成员 run 透传父 run 真实 `user_id`/`user_name`（不注入 cli:admin 假身份）
- **Session management**: `src/agent/session.py` — `AgentSession` dataclass, message history with TTL-based expiry
- **会话续聊恢复**: `Agent._restore_db_session_history()` — 进程重启 / worker 回收后首次承接旧会话时从 `messages` 表按序回填上下文(不重复落盘)
- **RunDispatcher**: `src/agent/runner.py` — 把 `Agent.run()` 的 团队/reflective/ReAct 三路选择收敛为单一分发入口(`agent/core.py` 不再堆叠形态 if/else)
- **Web 多用户 Worker 池**: `src/web/worker_pool.py` — `AGENT_WEB_POOL_SIZE>0` 时每个用户独立 Agent worker(`workspace/users/u_{uid}` + 独立 tracer/session/subagent), 隔离用户间实例/文件/记忆上下文; LRU 容量回收 + 空闲 TTL 清理; worker 注入归属身份 `owner_tag`/`owner_uid`(初始化前), 供平台轨按用户过滤「云端托管安装」
- **管理可观测 API**(admin): `/api/admin/stats|usage|sessions|sessions/running|sessions/{id}/messages`(查看/导出)、个人用量 `/api/usage`、个人工作台 `/api/my/overview`(总览卡片: 本人跨渠道会话/运行中/记忆统计, 与角色无关)
- **「运行中」会话**: `GET /api/agent/sessions/running`(本人)、`GET /api/admin/sessions/running`(admin 全量含姓名)。口径: 正在执行(占用 Agent worker / 流式中), 与 metrics `agent_running_streams` 同源; worker 池启用时以池登记 `WebUserWorkerPool.register_run/running_sessions` 为准(chat/chat_stream acquire 后登记、release 前注销), 池关闭时回退内存 `ChatSession.is_streaming`。**跨渠道**: 钉钉/飞书/webhook/定时等非 web 执行经 `MessageRouter.route` 前后写入进程级登记 `src/channels/run_registry.py`(除 cli 外全渠道, 与池登记/内存流式按 session_id 去重合并; 钉钉等跑在 root agent、`worker=false`, 不占池 worker)。每条含 conversation_id/channel/user/started_at/model/stage(`ChatSession.stage`, 流式 hook 事件更新工具/子代理阶段)。归属 tag 统一解析为 `{channel}:{uid}`(`WebServer._tag_uid`), 跨渠道同 agent 用户可见
- **Sub-agents**: `src/subagent_manager.py` — loads sub-agent templates from `config/agents/*/PROMPT.md`, reuses sessions by name
- **Memory**: `src/memory/manager.py` — DB 记忆按 `owner_id` 隔离(user 私有 + global 公共)
- **Learning**: `src/learning/learner.py` — self-learning module that triggers pattern extraction and skill creation
- **Storage**: `src/storage/storage.py` — unified SQLite with connection pool; single `config/data.db`; singleton via `init_storage(workspace, config_dir)`. 表: `messages`(含 `user_id/channel/conversation_id` 审计列)、`eventbus_events`、`autonomous_goals`、`kanban_tasks`、`scheduled_tasks`、`rbac_roles`(含 `permissions/data_scope`)/`rbac_users`/`rbac_user_identities`/`rbac_departments`、`memories/memory_proposals`、`web_tokens`、`usage_records`(含 `duration_ms/cache_*`, 聚合 `summarize_usage/usage_totals`)、`session_meta`、`webhook_tasks`(webhook 任务持久化+重启续跑)、`mcp_calls`(MCP 调用审计: server/tool/耗时/成败/归属, `record_mcp_call`/`query_mcp_calls`/`prune_mcp_calls`(启动清理 >30 天))、`capability_installations`(用户级云托管安装: user_id/capability_id/name/kind/enabled, UNIQUE(user_id, capability_id); `upsert/set_enabled/remove/list/installed_capability_names`)。启动自动做幂等 `ALTER` 迁移
- **Plugins**: `src/plugins/` — `BasePlugin` ABC; plugins loaded from `src/plugins/` dir, provide extra tools to agents
- **钉钉工具确认回路**(互动卡片, fail-closed): 纯逻辑在 `src/plugins/dingtalk/confirm.py`(`create_card_confirmer`/`CardActionRegistry`/payload 构建/超时解析); 钉钉每次 `router.route` 前后由 `confirm_scope` 装配——AUTO 模式临时降 DEFAULT, `agent.on_confirm` 按当前 run `conversation_id` 派发(并发 run 隔离、非钉钉 run 回退原回调); 写操作向触发人**私聊**发互动卡片(通用 AI 卡片模板, `callbackType=STREAM`), 按钮回调 topic `/v1.0/card/instances/callback`; 超时(默认 120s, `DINGTALK_CONFIRM_TIMEOUT` 覆盖)/发送失败/回调主题不可用一律拒绝, 渠道不可用时最终回复固定文案; 同一 request_id 首个裁决生效(重复点击幂等)
- **MCP dingtalk 办公 API**: `mcp_server/src/dingtalk.py` 除消息/通讯录/卡片外, 新增审批(`dingtalk_approval_start/instance/tasks/action`)、待办(`dingtalk_todo_create/update/list`)、日程(`dingtalk_calendar_create_event/list_events/freebusy`); 注解: 查询 readOnly、创建/更新非破坏写、审批同意/拒绝 destructive; 统一 `_api` + 响应截断防超长; v1.0 对「应用缺权限」也可能统一回 HTTP 503(实测), `_api` 对 503 按 0.5s/1s 退避重试 2 次, 持续 503 文案附权限申请提示; `dingtalk_approval_tasks` 主 503 时旧版两段式回退: `topapi/process/listbyuserid` 列模板(≤50) → 逐模板 `topapi/processinstance/listids`(必带近 90 天 `start_time`/`end_time` 毫秒时间戳; status 0→RUNNING / 1→COMPLETED, 单模板失败计数跳过并透出 `first_error`, 全失败且首错 88/60011 直接返回权限文案); 列模板报 88/60011 解析 scope+申请链接, 成功返回实例 ID + process_count/failed_processes(详情需 `dingtalk_approval_instance`); 待办/日程 6 个 unionId 类工具(`dingtalk_todo_create/update/list`、`dingtalk_calendar_create_event/list_events/freebusy`)支持传钉钉 userId(纯数字), 经 `topapi/v2/user/get` 自动换算 unionId(进程内缓存 `_UNIONID_CACHE`, 查不到给明确错误), `dingtalk_get_user_detail` 返回 unionid; 用户标识参数统一命名: `dingtalk_userid`/`dingtalk_userids`(钉钉用户ID, 非工号/本系统 userId)、`dingtalk_unionid`/`dingtalk_unionids`(钉钉 unionId, 次级参数如 `dingtalk_creator_unionid`), JSON body 里的钉钉字段名(`userid`/`userIds` 等)不变; 工号兜底换算: `_resolve_dingtalk_userid`(approval 任务/操作/评论/发起与 `dingtalk_get_user_detail`)与 `_resolve_unionid` 在 `topapi/v2/user/get` 失败时查通讯录索引(`_directory_index`: 部门 listsub 全量 + `topapi/v2/user/list` 分页, TTL 1800s 懒构建, 构建失败返回空索引不抛), 把工号自动换成真实 userId/unionId; `dingtalk_userids` 列表参数暂不换算
- **MCP servers**: `src/mcps/manager.py` — launches external MCP tool servers defined in `config/mcp_servers.json`; 每 server 可选治理字段(缺省保持旧行为): `timeout_seconds`(工具调用超时, 默认60; remote_terminal 生产配 300)、`connect_timeout_seconds`(默认30)、`max_reconnect_attempts`(默认3)、`max_concurrency`(默认4, 信号量排队)、`risk_overrides`(原始工具名→`read|write|destructive`, 覆盖工具注解); 工具级风险由 `annotations`(readOnlyHint/destructiveHint) 映射(read/write/destructive/unknown), 经 `MCPManager.tool_risk/tool_server/tool_raw` 暴露: `PermissionChecker` 按模式处理(PLAN 拒写类、SMART 仅 destructive 需确认/write 放行、DEFAULT destructive/write 需确认、AUTO 放行; 注: PLAN 对无注解 `unknown` 的 MCP 工具不拦截, 不构成只读保障, 需靠白名单/风控兜底), destructive MCP 工具结果命中敏感改道; 自研 server 工具已批量补注解(`mcp_server/src/*.py`, 写操作 destructive、读 readOnly、消息发送/配置类非破坏写); 每次 MCP 调用(成功/失败)落 `mcp_calls`(耗时/结果大小/归属), 状态经 `MCPManager.status_all()` 聚合(各实例状态/失败清单/重连次数), `/healthz` 带 `mcp` 字段(不鉴权), `GET /api/admin/mcp` 返回完整状态、`GET /api/admin/mcp/calls?limit=&server=&tool=` 查询审计(均需 `admin.monitor`, limit ≤1000)
- **DevOps MCP(自研, 默认未挂载)**: `mcp_server/src/gitlab.py`/`gerrit.py`/`jira.py` — 内网 GitLab REST v4/Gerrit REST/Jira Server-DC REST v2 读写工具(共 53 个; 查询 readOnly、创建/更新非破坏写、合入/取消/删除/评审投票 destructive); GitLab 支持 `GITLAB_TOKEN` 或 LDAP 会话登录(`GITLAB_USERNAME`+`GITLAB_PASSWORD`), Gerrit 走 `/a/`+`GERRIT_HTTP_PASSWORD`(缺省匿名只读; XSSI 剥离、diff 大文件排除), Jira 支持 `JIRA_TOKEN`(Bearer)或 Basic; 口令均可回退 `IT_SYSTEM_PASSWORD`, 地址默认内网(`GITLAB_URL`/`GERRIT_URL`/`JIRA_URL`); 本次未注册进任何 `mcp_servers.json`, 启用示例与 env 见 `mcp_server/README.md`, 测试 `tests/unit/test_mcp_servers_devops.py`
- **平台 MCP 轨(市场能力)**: `src/mcps/platform.py` — `MARKET_BASE_URL`+`MARKET_SERVICE_TOKEN` 齐备才启用(缺失=关闭, 仅本地 MCP); `PlatformMCPClient` 拉 `GET {MARKET_BASE_URL}/api/capabilities/sync`(Bearer 服务令牌)筛 `type=mcp` 且 `distribution in (remote,both)`(local 网关 403 直接跳过), 经能力网关 `/api/mcp-gateway/relay/{name}/stream`(Streamable HTTP, `name@version` 钉版本; 有 `gateway.stream_url` 时优先)用 MCP SDK 连接并 `list_tools`; 工具暴露名 `platform__{能力}__{工具}`(本地/保留名优先, 平台同名跳过), 风险映射与本地同源(注解→read/write/destructive/unknown); `MCPManager.attach_platform` 组合后 LLM 工具表、`PermissionChecker` risk_resolver、`mcp_calls` 审计(server=`platform:{能力}`, 带 conversation/user 归属)、`/healthz` 与 `/api/admin/mcp`(独立 `platform` 块, 每能力行 `source=platform` 带 version/last_refresh)全覆盖; 根 agent 与 worker 池用户 worker 挂接(子代理不重复建连), 周期刷新(`MARKET_PLATFORM_REFRESH_SECONDS` 默认 300, `MARKET_PLATFORM_TIMEOUT` 默认 60, 非法回退)随 Agent 初始化启动、cleanup 取消; sync 失败仅告警保留既有连接, 单能力失败隔离(连接分小批 ≤2、批间隔 0.3s 推进, 避免瞬时并发拉起上游; 失败项 30s 起指数退避快速重试、封顶刷新周期、成功清退避计数), 版本变化重连; last_error 经 `unwrap_error` 解包 ExceptionGroup 展示根因; 令牌不落日志/状态。**按用户身份(act-as)**: `MARKET_ACT_AS=1/true/yes`(默认关)且平台轨启用时, worker 池(需 `AGENT_WEB_POOL_SIZE>0`; 池关闭仅打 WARNING, root 保持服务令牌全量)为每个 worker 注入 `platform_act_as`=该用户的市场用户名(= rbac 工号; `web:{uid}` 数字 uid 查 `rbac_users.name`, 非数字(SSO sub)直用, 空/查不到回退服务令牌并可见 WARNING), sync 与 relay 请求均带 `X-Act-As-Sub`(每 worker 恒定该用户视角: sync 只回其「已订阅∩可见∩可访问∩非 local」, relay 按其门禁/限流/密钥注入; 仅市场服务令牌身份生效, 令牌需 `gateway`+`sync` scope)。该模式**覆盖 worker 池用户**(web + 钉钉/飞书单聊, 见 `MessageRouter` 非 web 按用户隔离); 群共享根/其它跑在 root 的渠道仍为服务令牌视角。**用户级云端托管安装(P3)**: worker 另注入 `owner_tag`/`owner_uid`(`src/web/worker_pool.py` 在 initialize 前), 平台轨构造时传 `install_filter` 闭包(读 `capability_installations` 该用户「已安装且启用」能力名, 读取异常按空集 fail-closed), sync 后仅保留集合内能力(空集=不出平台能力); root 不传(服务令牌全量)。安装/启停后调 `Agent.refresh_platform_installations()`(→ `platform.refresh_once()`, 异常安全)让工具集立即变化; 本地安装(写 `config/mcp_servers.json`)仍仅管理员, 不受影响
- **市场逐请求代授权工具(OBO)**: `src/tools/market_runtime.py`(`market_runtime`) — 走市场 `/api/runtime/tools/{cap}/invoke` 或 `/api/runtime/agents/{cap}/tasks`, 逐请求携带 `X-Act-As-Sub` = 当前 `RunContext.user_id` 的市场用户名(`resolve_market_act_as`), 由市场 `require_runtime_access_obo` 做 **actor ∩ subject** 求交、用量归因给 subject; 无 subject 或市场未配置一律 fail-closed。与平台 MCP 轨(持久会话、act-as 每 worker 恒定)互补: 全局单例(如零号员工)无法按请求切换会话身份, 故**用户级能力**走本工具逐请求代授权, **服务级/全局能力**走平台 MCP 轨。仅市场配置齐备时保留(`Agent._init_market_runtime` 未配置则从工具表移除); 作为内置工具受 RBAC `check_tool` 约束(需被授予 `market_runtime`)。**能力绑定(binding)**: 市场能力新增 `binding` 元数据(`user`|`service`, 默认 `service`); 平台 MCP 轨 `parse_sync_capabilities` 跳过 `binding=user` 的能力(持久会话无法逐请求变换身份), 改由 `market_runtime` 按 subject 调用 `/api/runtime/*`
- **能力市场代理 + 云端托管安装(Web, P4)**: `src/web/routers/market.py`(`build_market_router(server)`) — agent 不持用户 SSO token, 统一服务令牌 `Bearer` + 按需 `X-Act-As-Sub`(市场用户名 `resolve_market_act_as("web:{uid}")`): 浏览 `GET /api/market/capabilities` 代理 `GET /api/capabilities` 并 merge `joined`(act-as `/api/my/capabilities?scope=added`)与 `installed`(本地 `capability_installations`); 详情代理 `GET /api/capabilities/{id}`(schema 自带 plugin `components`, 无需 include_components 辅助); join/leave act-as 代理 `POST/DELETE /api/my/capabilities`; 安装/启停/卸载(`GET/POST /api/market/installations`、`PATCH/DELETE /api/market/installations/{id}`)写本地表并调 `server.refresh_user_platform(uid)` 刷新已存在 worker(不新建, 无 worker 则下次会话生效); 校验 type=mcp + 非 local + 市场已加入; 市场未配置 503 `{"error":"能力市场未配置"}`; 管理端 `GET/PUT/DELETE /api/admin/local-mcp[/{name}]` 读写全局 `config/mcp_servers.json`(白名单字段, 名称以路径为准), 写后 `manager.reload_all()`, 需 `admin.mcp_local`(本组不含本地包下载)
- **渐进披露(工具搜索)**: 纯逻辑 `src/agent/tool_search.py` + 内置工具 `src/tools/search_tools.py` — 远程工具(本地/平台 MCP + 插件)过多时不再每轮全量塞给 LLM。模式 `AGENT_TOOL_SEARCH`=auto(默认)/always/off(非法值告警回退 auto), 阈值 `AGENT_TOOL_SEARCH_THRESHOLD` 默认 40(远程工具数 > 阈值触发 auto, 边界 40/41; 非法值回退)。渐进模式下工具表=**核心工具**(内置/技能, 恒注入, 含 search_tools) + 该**对话根**(`RunContext.conversation_id`, web/钉钉/子代理一致)已激活且当前可用的远程工具; 每次 run 开始把说明(`progressive_hint`: "连接器的更多工具需先用 search_tools 搜索；已激活：…")追加进 system prompt 的 dynamic 段(static 前缀保持可缓存)。`search_tools(query, limit≤20)` 在当前可用远程工具中按名称精确 100/名称包含 10/来源 5/描述 2 打分检索(同分按名称升序), 命中即激活并返回"已激活, 下一轮可直接调用"清单; 未命中给换词提示。激活状态按对话根持久化到 `session_meta.active_tools`(JSON 数组; `storage.get_active_tools/set_active_tools/delete_active_tools`, 压缩摘要 UPSERT 不覆盖; 存储不可用/异常回退进程内内存); server 断开/能力下线时注入前过滤但不清理激活集(恢复后自动可用)。`search_tools` 是内置工具(kind=只读, 无需确认), 不参与 `mcp_calls` 审计, 激活写 INFO 日志; 工具注入集合变化时打一条 `工具注入: 核心 X + 激活 Y + search_tools（渐进 auto/always）`(全量时 `工具注入: 全量 N（渐进未启用, 模式 off/auto）`), 便于线上验证
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

> Worker 池启用后: web 与钉钉/飞书**单聊**各自独立工作区 `workspace/users/u_{uid}`; 群共享根/CLI/其它渠道仍用共享 `workspace/`(该服务端共享目录仅 admin 可见, `GET /api/workspace/files`)。非 worker 模式(默认)则所有渠道共享 `workspace/`。

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

- **代授权(on-behalf-of, 零号员工基座)**: `RunContext` 新增 `actor_id`/`actor_role`(代授权执行者服务身份, 空=用户直连), 真实提问者仍放 `user_id`/`role`/`user_role`/`user_department`(subject); `Agent.run(..., actor_id=, actor_role=)` 显式传入、子代理经 ContextVar 继承。授权在 `executor.execute_tool_safe` 求交: subject `check_tool` 通过后, 若 `actor_role` 非空再校验 actor `check_tool`, **有效权限 = actor grant ∩ subject grant**(个人 Agent actor 为空, 行为不变); 审计 `mcp_calls` 增 `actor_id`/`subject_id` 列(启动幂等迁移), `HookContext` 同步携带。种子角色 `supreme`(allowed_tools/agents=`*`, Web 权限为空)供零号员工 actor 门禁使用; `web/sso_auth.exchange_token`(RFC 8693)把 subject token 换成下游(rag/market)受众令牌。market 侧 `/api/runtime/*` 已支持 `X-Act-As-Sub` 并做 actor∩subject 求交。服务 token 收窄与 dashboard JIT 角色透传属下一阶段(见 `docs/零号员工与个人Agent演进方案.md`)。
- **对话优先(Conversation-first)**: `messages.conversation_id` 定义“一个用户对话”。顶层 run 的 `session_id` 即对话根(`web:{uid}:{rand}`); 子代理/团队成员运行继承同一 `conversation_id`, 其内部上下文为确定性线程 `session_id = <对话>#<agent>`(`src/agent/core.py` RunContext 下传)
- **列表/续聊/审计按对话聚合**: `/api/agent/sessions/history`、`/api/admin/sessions` 走 `storage.list_conversations()`(主对话条数 + thread_count), 不再按 agent 碎片列出; 审计可经 `/api/admin/sessions/{conv}/threads` 下钻内部线程。普通用户「我的会话」= 同一 agent 用户跨 web/钉钉/其它 `tag:{uid}` 渠道合并(`storage.list_conversations_for_agent_user()`), 每条带 `channel` 字段(供前端渠道徽标; 个人「对话」页侧栏即此跨渠道历史, 钉钉等外部会话点击**只读**查看)。**个人口径约定**: `/api/sessions`、`/api/agent/sessions/history`、`/api/memories`、`/api/scheduler/tasks` 默认个人口径(admin 亦只看自己): `/api/memories` mine=仅本人私有(user scope, **不含 global 公共**), `/api/scheduler/tasks` mine=仅本人创建的 DB 任务(排除 static 系统级与他人); 仅当 admin 显式传 `scope=all`(`/api/memories` 为 `view=all`) 才返回全站(定时含 static、记忆含 global+全部人私有), 供「运行监控」运维组使用。**续聊写回仅允许 web 前缀会话**: `/api/chat` 与 `/api/chat/stream` 对非 web `session_id` 返回 400(钉钉等外部渠道历史只读, 由对应渠道插件续聊, 不经过 /api/chat); 属主读取任意渠道消息(含 DB 回退)不受限
- **子代理按(对话, agent)隔离**: `SubagentInstance.conversation_id` + `_name_to_session` 仅在相同对话内复用, 杜绝跨对话/跨用户串上下文; 老随机子会话由启动迁移以自身 session 兜底为对话根
- **会话命名空间**: web 渠道 session_id 服务端强制 `web:{uid}:{rand}`; 非属主访问一律 404
- **审计落盘**: `messages` 表带 `user_id/channel/conversation_id` 列; 会话内容全部落库,admin 可经 `/api/admin/sessions/{id}/messages` 查看/导出
- **用量/性能**: LLM 调用写入 `usage_records`(含 `duration_ms`, 供 P50/P95); 个人用 `GET /api/usage`,管理端用 `/api/admin/usage`
- **内存/文件隔离**: 记忆按 `owner_id` DB 隔离; worker 池启用后文件按 `workspace/users/u_{uid}` 隔离
- **角色 + 部门权限控制**(2026-09-15 强化): `rbac_roles` 新增 `permissions`(Web 管理权限键, `*` 通配) 与 `data_scope`(`all` 全站 / `department` 本部门 / `self` 仅本人); 内置 `admin` 恒 `["*"]`+`all`、`default` 恒 `[]`+`self`(按角色名特殊处理, 老库升级安全)。`allowed_tools` 支持只读限定条目 `{tool}:read`(如 `file:read`/`shell:read`), 由 `PermissionChecker.classify_access`(file 读操作/shell 读前缀/MCP read 注解 → read, 其余保守按 write)判定后拒绝写调用; 旧条目 `{tool}` 仍读写全放行。权限键目录见 `security/rbac.py:WEB_PERMISSIONS`(admin.users/roles/departments/monitor/logs/memories/scheduler/workspace/mcp_local)。鉴权统一走 `web/security.py`(`get_authz`/`require_perm`/`has_permission`/`scope_department`), 主服务 `_require_perm`、Router 用 `perm_or_403`; `admin.*` 系列原「凭 role==admin」判定改为权限判定, 并补齐此前**无守卫**的 RBAC 端点(roles/users GET·DELETE·identities)。数据隔离: `data_scope=department` 的管理员仅可见/可管本部门成员(用户列表、管理端会话/用量/运行中/定时任务全量视图均按 `rbac_users.department` 过滤; 存储层 `query_usage/summarize_usage/usage_totals` 支持 `uids` 过滤), 且不得分配超管/全站范围角色、不得跨部门调整成员。`rbac_departments` 表 + `/api/rbac/departments` CRUD(有成员时禁删), 部门改名同步成员归属。前端 `/api/auth/me`·login 回包带 `permissions`/`data_scope`/`department`; 菜单/路由按 `hasPerm(key)` 收敛(`main.ts` `perm`/`permAny`), 「用户与权限」页含用户/角色/部门三个 Tab(按权限显示)
- **Worker 池**: `AGENT_WEB_POOL_SIZE>0` 启用(每用户独立 Agent), 容量守护: 溢出上限
  `AGENT_WEB_POOL_OVERFLOW`(默认=容量, 硬顶2x; 0=不许溢出), 饱和时短暂等待
  `AGENT_WEB_POOL_ACQUIRE_TIMEOUT` 后返回 503(不再无界扩容); 详见 `docs/refactor-blueprint.md`
- **非 web 单聊按用户隔离(2026-09-25)**: `MessageRouter.route` 在通道为钉钉/飞书单聊且能解析出数字 uid(`{channel}:{uid}`)时, 经注入的 worker 池取 `{channel}:{uid}` worker(工作区 `workspace/users/u_{uid}`)执行, 并在运行期把 root 的 `on_confirm`/权限模式同步到 worker(钉钉确认卡片不失效)、结束恢复; 群共享根(`dingtalk_group`)/未解析身份/池饱和分配失败 → 回退 root 共享实例(告警)。WebServer 建池后由 `main.py` 经 `router.set_worker_pool(pool)` 注入。**注**: 飞书插件当前不传 `user_id`(仍为合成 `feishu:admin`), 故暂不隔离——补齐飞书身份后自动生效
- **文档同步**: 改动代码需同步维护本文件与 `docs/`、`frontend/README.md`

## Skill Lifecycle — Automatic Routing

The agent uses the `skill` tool to load structured workflows. Skills follow the lifecycle: **DEFINE → PLAN → BUILD → VERIFY → REVIEW → SHIP**. Before ANY action, check skill applicability:

### 技能可见性（部门/角色）

`SKILL.md` frontmatter 可选 `departments: [..]` / `roles: [..]`（列表；`roles` 值为 agent rbac 角色名如 `admin`/`default`）：缺省或空列表=该维度不限；两维度都非空须同时命中（AND，与市场 `services/access.py` 一致）；`user_role == "admin"` 直通（忽略限制，同市场 admin 早退）；用户部门/角色为空时不命中受限维度（fail-closed，受限技能不可见且执行前二次校验拒绝，disabled 技能点名调用同样拒绝）。过滤作用于 `skill` 工具描述中的 `<available_skills>`、团队子代理/CLI 提示中的技能清单及执行入口；部门仅 web 渠道经 `_get_authz` 解析传入，角色取渠道显式传入的 `user_role`（web/钉钉/定时均显式传入 rbac 角色，权限用 `role` 与技能身份分离）；未解析身份的渠道（飞书/webhook 等）按空处理=受限技能不可见。frontmatter 类型非法（非列表或列表项全非法）按 fail-closed 处理：非 admin 用户不可见并记 WARNING。样板见 `tests/unit/test_skills.py`。

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
- **`MARKET_ACT_AS`(平台轨按用户身份)同为启动期 env** — 生产改开/关必须重建镜像(同平台轨); 仅 worker 池启用(`AGENT_WEB_POOL_SIZE>0`)时逐 worker 生效, 池关闭仅打 WARNING 并保持 root 服务令牌全量; 依赖市场服务令牌含 `gateway`+`sync` scope 且网关支持 `X-Act-As-Sub`(缺失/未准入时该用户平台能力为空, 属回退)
- **`config/memory/` and `config/sessions/` are gitignored** — they contain runtime state
- **`docs/plans/` is gitignored** — design docs live there but are not tracked
- **Sub-agent names are Chinese** (e.g. `设备运维`) — this is intentional, not a mistake
- **LLM 端点以 `config/config.json` 的 `llm.endpoints` 为准** — 每项含 `model`/`base_url`/`api_key`(支持多端点); 生产统一指向 router 公网入口(`https://ai.rosiwit.com/v1` → router 网关), 其它环境改 config.json 即可(不再依赖 OPENAI_BASE_URL/DashScope 默认值)
- **`max_retries=0` on OpenAI client** — all retries are handled by our application-level retry logic in `LLMClient`, not by the httpx SDK
- **LLM timeout is configurable**: `LLM_TIMEOUT` (default 300s, read timeout) and `LLM_CONNECT_TIMEOUT` (default 30s, connection timeout)
- **MCP SDK v2(Python)** — 已迁 `mcp>=2.2,<3`(客户端与自研 server 同步)：`FastMCP`→`MCPServer`(`from mcp.server.mcpserver import MCPServer`)、协议字段 snake_case(`input_schema/is_error/structured_content`)、**同步 handler 跑 anyio worker 线程**(涉及事件循环或共享可变状态的工具必须 `async def` 或加锁)、SDK 网络改用 `httpx2`(日志记录器 `httpx2`/`httpcore2`)、`nest_asyncio` 已移除、**stdio 关闭语义**: 先关 stdin 等优雅退出再升级杀进程树(POSIX 优雅退出不杀孙进程, 超时对新进程组 SIGTERM→SIGKILL; Windows Job Object 直接终止进程树), **客户端连接/收尾走专属连接任务**(anyio cancel scope 进出同任务, 关闭 shielded 等待不被取消打断)
- **MCP servers 默认启用状态不一** — `config/mcp_servers.json` 中 `time`/`fetch` 默认 `"enabled": true`(纯离线/只读)；`filesystem`/`git`/`postgres`/`mysql_query` 默认 `"enabled": false`，需显式开启并配置白名单或连接串(`FS_MCP_ROOTS`/`GIT_MCP_ROOTS`/`PG_MCP_DSN` 等)；`filesystem`/`git` 写操作另需 `*_ALLOW_WRITE=true`。W3 新增内置 server: `time`(时区)/`fetch`(抓取, SSRF+CGNAT 防护)/`filesystem`(受限目录, symlink 不跟随)/`git`(仓库白名单+gitdir 越界防护)/`postgres`(只读事务+行数/超时上限)，详见 `mcp_server/README.md` 与配置内中文描述
- **`AGENT_WEB_POOL_SIZE` 默认 0** — 多用户部署需显式设 >0; 启用后每个用户首次请求会触发 worker 冷初始化; 回滚/单实例直接置 0
- **DB 迁移幂等自愈** — `messages.user_id/channel`、`usage_records.duration_ms/cache_*`、`rbac_users.display_name` 由启动时 `ALTER` 自动补齐并回填一次历史(web:{uid}); 无需手工
- **SSO 账号模型** — `rbac_users.name` 恒为工号(sub, 唯一身份键), 中文显示名放 `display_name`; `_sso_ensure_user` 未按工号命中时会按 `claims.name`(中文) 找老账号并把其 name 改成工号(双账号自动合并, 保留 role/dept/status/钉钉绑定/历史); `claims.dingtalk` 登录成功后自动 `bind_identity`(幂等), 钉钉渠道免人工开号。展示类接口(name/owner)同时返回 `display_name` 字段, 前端优先用, 内部归属仍按 name=工号/tag
- **SSO 双轨配置** — 资源轨(校验别人传来的 token): `SSO_ISSUER`/`SSO_AUDIENCE`/`SSO_JWKS_URI`; 登录轨(浏览器授权码流程 `/api/auth/sso/start` + `/api/auth/sso/callback`): 追加 `SSO_CLIENT_ID`/`SSO_CLIENT_SECRET`/`SSO_REDIRECT_URI`/`SSO_REDIRECT_TARGET`(子路径+hash 路由部署时为 `/agent/#/login`)。`is_configured()` 只看 issuer, 故登录轨配不全时 `/sso/start` 会 302 但换 token 必失败 — 两轨要一起配。容器内解析不到公网域名时需 `--add-host auth.xzrobot.com:192.168.31.45`(已固化在 `build.sh`), 否则回调在 `issuer/token` 一步失败
- **权限统一用 RBAC(2026-09-25)** — 所有授权收敛为「角色→权限键」: 服务身份(X-Service-Token)建模为种子角色 `service`(`permissions=["admin.users"]` 最小权限, `allowed_tools=[]`, `data_scope=all`, 可在角色管理调整), 不再硬编码 admin 旁路; `_get_authz` 对其解析 `service` 角色, `_require_perm` 以权限键判定(`/api/auth/set-password` 由 `admin.users` 判定); 会话/看板/记忆等处的 `role=="admin"` 字面判定替换为 `has_permission(user,"*")`(admin 行为不变, service 按角色权限); `web/security._service_identity()` 解析 `service` 角色权限(DB 不可用时按最小 `admin.users` 兜底); 老库未定制的 `service` 行(仍为 `["*"]`)会幂等迁移为最小权限, 已定制则保留。概览见 `docs/rbac-统一权限模型.md`
- **`X-Service-Token` 服务身份(管理面)** — `_is_service_request` 命中的请求在 `_get_authz` 按 `{uid:0, role:service}` 处理, 权限由 RBAC `service` 角色表达(默认 `admin.users`: 供网关/工作台开号与改密), 只下发给可信服务; `/api/chat` 与 `/api/chat/stream` 未鉴权一律 401(走 `_get_authz`, 不再回落 `web:anon`; `WEBUI_DISABLE_AUTH=1` 仍由 `_get_auth` 处理)。**对话面**: 服务身份 `service` 角色无工具权限(工具全拒), 不再隐式 admin; 服务若要代表用户执行必须携带用户身份(见代授权/OBO)
- **测试勿在模块导入时改环境** — `WEBUI_DISABLE_AUTH` 等必须用 autouse fixture(monkeypatch)注入; 导入期设置会泄漏给后续测试文件, 曾使 `test_sso_auth` 双轨鉴权用例假失败
- **会话隔离依赖命名空间** — 非 web 前缀的旧会话无法回填归属, 对非 admin 普通用户不可见(安全优先), admin 仍可审计; 改造前旧格式钉钉会话(dingtalk:{staff_id})同理保持不可见
- **跨渠道合并仅限同一 agent 用户** — 钉钉改造后归属 tag 为 `dingtalk:{agent_user.id}`(与 web:{uid} 同 rbac 用户), web「会话」页才会合并展示并带渠道徽标; 钉钉会话命名/复用(2026-09-08 群模型 Phase1): 单聊根 `dingtalk:{uid}:{rand}`、群共享根 `dingtalk_group:{safe}:{hash}:{rand}`(前缀即类型)。同 scope(单聊=agent_uid / 群聊=cid 规范前缀) 复用「最近单聊根」或 `dingtalk_scopes` 持久指针, **跨进程重启可续根**(指针/最近根落库); `/new` 开新根并覆写指针。群共享根整群一个上下文、群间并发/群内串行(现有 `_conv_lock`), 群 run 不注入触发人私有记忆(记忆工具群内置空属主只读 global), 群根历史参与者(根下出现过 `dingtalk:{uid}` 的成员)+admin 可见; 恢复上下文能力仍按 `_restore_db_session_history`。**Phase2(敏感结果私聊送达)**: 敏感工具清单默认 = mysql_query MCP 三工具 `list_tables`/`describe_table`/`execute_query`(config.json `sensitive.tools` 可覆盖, 读取集中 `agent/sensitive.py`); 工具执行层命中清单 → 给当前 run 打 `sensitive_hit` 标记(RunContext/AgentResult, 子代理调用链经 `agent/core.py` run 结束时汇聚到顶层); 钉钉群共享会话(`dingtalk_group:`)且本轮命中敏感 → 最终回复文本不落群, 经 oToMessages 私聊送达触发人(staff_id), 群内仅发 `GROUP_SENSITIVE_PRIVATE_NOTICE` 占位; 单聊与非敏感工具群内照常直接回复。**Phase2 收尾(敏感内容不落群根)**: 群共享根每一轮 run 的落库消息带整轮标记 `messages.round_id`(顶层群 run 用自身 run_id, 子代理线程继承); 命中敏感的那一轮在 `agent/core.py` run 收尾时经 `storage.relocate_round_to_dingtalk_private` 把 `conversation_id=群根 且 round_id=该轮` 的整轮(敏感工具产出 + 含敏感结果的最终回复 + 该轮触发问题)从群根 `messages` 搬到私有旁路表 `dingtalk_private_messages`(按 `dingtalk:{uid}` owner_tag 关联, 触发人本人可查、他人/群根历史不可见、上下文恢复不含该轮), 并把该轮从群共享内存上下文回滚; 非敏感群轮/单聊照常实时落库不受影响。触发人读取路径: `GET /api/my/dingtalk/private-rounds`(个人口径, admin 亦只看自己) + `storage.list_dingtalk_private_rounds/messages`