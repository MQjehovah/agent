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
- **Session management**: `src/agent_session.py` — `AgentSession` dataclass, message history with TTL-based expiry
- **Sub-agents**: `src/subagent_manager.py` — loads sub-agent templates from `config/agents/*/PROMPT.md`, reuses sessions by name
- **Memory**: `src/memory/manager.py` — daily memory files + long-term `memory.md` + `shared_knowledge.md`
- **Learning**: `src/learning/learner.py` — self-learning module that triggers pattern extraction and skill creation
- **Storage**: `src/storage.py` — unified SQLite with connection pool; `Storage` manages all tables (messages, eventbus_events, autonomous_goals, kanban_tasks) in a single `data.db`; singleton initialized via `init_storage(workspace, config_dir)`
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
```

## Key Conventions

- **All source is under `src/`** — there is no package namespace; modules import each other directly (e.g. `from agent import Agent`)
- **Tests add `src/` to `sys.path`** manually (`sys.path.insert(0, ...)`) — no `pyproject.toml` package install
- **Language**: Code comments, log messages, and workspace content are in Chinese; variable names and docstrings are English
- **Environment**: `.env` loaded via `python-dotenv` at startup; falls back to `.env.example` if `.env` missing
- **Workspace PROMPT.md** uses frontmatter (`---\nname: ...\ndescription: ...\n---`) parsed by `utils/frontmatter.py`
- **Permission modes**: `default` (confirm writes), `auto` (allow all, for containers), `plan` (read-only) — set in `Agent.__init__`
- **Logging**: Uses `rich.logging.RichHandler` with aligned logger names; API calls logged to `logs/api_YYYYMMDD.log`
- **Sandbox**: Optional sandbox via `config/sandbox.json` (process or Docker mode). Intercepted at `Agent._sandbox_intercept()` — tools remain unaware of sandboxing
- **Team pipeline**: `TeamOrchestrator` supports `default`/`feedback`/`auto` modes. `feedback` mode enables dev↔test feedback loops with automatic retry. `auto` mode uses LLM to dynamically generate pipeline stages

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