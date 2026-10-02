# 工具参考（Tool Reference）

> 本文档列出 agent 当前可用的工具、参数、可见性与权限约定。代码为准：`src/tools/`（自动发现），动态工具来自技能/检索/MCP/插件/市场。

## 一、总览

- **注册**：`ToolRegistry.auto_discover()` AST 扫描 `src/tools/*.py`，实例化所有 `BuiltinTool` 子类并注册（跳过 `__init__.py`/`__pycache__`；非 `BuiltinTool` 的模块如 `task.py` 不注册）。
- **返回信封**：统一 JSON `{"success": bool, "error": "..."}`（成功带业务字段；`market_*`/`read_image` 保留旧 `ok` 键做兼容）。
- **执行闸门**：所有工具调用经 `agent/executor.py:execute_tool_safe`（权限 → RBAC → 审批 → 沙箱 → 审计 → 执行 → 后置钩子），不可绕过。
- **渐进披露**：核心工具恒注入；远程工具（MCP/插件/市场）在数量超过阈值（`AGENT_TOOL_SEARCH_THRESHOLD`，默认 40）时经 `tool_search` 检索激活。

## 二、内置工具（20 个，核心恒注入）

| 工具 | 参数 | 用途 | 文件 |
|---|---|---|---|
| `file` | `operation`(read/write/append/delete/exists/list), `path`, `content`, `encoding`, `offset`, `limit` | 通用文件读写；读默认 200 行、可 `offset`/`limit` 分段 | `tools/file.py` |
| `edit` | `path`, `old_string`, `new_string`, `line`, `edits`(`[{file,old_string,new_string,hash}]`), `replace_all`, `workspace` | 精确字符串替换（原文偏移、保留 CRLF/行尾）；`edits` 每项可带 `file` 做跨文件原子编辑 | `tools/edit.py` |
| `apply_patch` | `patch`, `dry_run` | 应用 git 风格 unified diff，原子落地；失败整体不写 | `tools/apply_patch.py` |
| `glob` | `pattern`, `path`, `limit` | 文件名 glob（`*`/`?`/`**`） | `tools/glob.py` |
| `grep` | `pattern`, `path`, `file_pattern`, `case_insensitive`, `limit`, `context_lines` | 内容正则搜索（返回行号与上下文） | `tools/grep.py` |
| `code_search` | `query`, `target`(definition/callers/references/all), `file`, `symbol_type`, `workspace` | Tree-sitter AST 定义/调用/引用（多语言） | `tools/code_search.py` |
| `code_diagnostics` | `path`, `languages`, `timeout` | 按项目类型跑 ruff/mypy/eslint/tsc/go vet/cargo check，结构化问题 | `tools/diagnostics.py` |
| `git` | `operation`(status/diff/log/commit/checkpoint/rollback), `message`, `path`, `staged`, `limit`, `name`, `all` | Git 操作；`rollback` 破坏性 | `tools/git.py` |
| `shell` | `command`, `timeout`, `cwd`, `env`, `max_chars` | 执行命令；默认 30s、输出截断、危险命令黑名单 | `tools/shell.py` |
| `web_search` | `query`, `limit` | 多引擎搜索（SearXNG/Tavily/Serper/Bing） | `tools/web.py` |
| `web_fetch` | `url`, `max_chars` | 抓取 URL 转文本 | `tools/web.py` |
| `read_image` | `ref`, `question` | 查看附件/工作区图片（视觉模型 + OCR） | `tools/read_image.py` |
| `task` | `task`, `template`, `name`, `session_id`, `system_prompt`, `tools`, `mcp_servers`, `keep_alive` | 子代理/团队委派（真实调度在内核 `execute_subagent`） | `tools/subagent.py` |
| `todowrite` | `todos`(`[{id,content,status,priority}]`), `filter_status` | 待办列表（整体替换） | `tools/todo.py` |
| `ask_user` | `question`, `options`, `default` | 向用户提问/确认（多渠道桥接） | `tools/ask_user.py` |
| `memory` | `action`(save/search/list), `content`, `category`, `query`, `memory_type` | 长期记忆读写（按 `owner_id` 隔离） | `tools/memory.py` |
| `whoami` | — | 当前用户身份画像（姓名/工号/部门/角色/钉钉 userId/渠道/uid） | `tools/whoami.py` |
| `tool_search` | `query`, `limit` | 在远程工具中检索并激活 | `tools/tool_search.py` |
| `market_search` | `query`, `kind`(agent/skill/mcp/tool/all), `limit` | 能力市场发现（按用户 token） | `tools/market_search.py` |
| `market_execute` | `capability`, `kind`(tool/mcp/skill/agent), `tool`, `params`, `task` | 执行市场能力（按用户 token）；`kind="agent"` 即委派专家 | `tools/market_execute.py` |

> `market_*`（`market_search`/`market_execute`）仅在市场配置齐备（`MARKET_BASE_URL` + `MARKET_SERVICE_TOKEN`）时保留；否则启动时从工具表移除。

## 三、动态 / 条件工具

| 工具 | 触发条件 | 说明 |
|---|---|---|
| `skill` | 加载到技能目录时 | 加载技能正文；`<available_skills>` 按用户部门/角色 + agent 作用域过滤，执行前二次校验 |
| `knowledge_search` | 配置 `RAG_BASE_URL` 时 | 按用户 token 调 RAG `/api/search` |
| 本地 MCP 工具 | `config/mcp_servers.json` 中 `enabled: true` | 名称由 `list_tools` 决定；风险按注解映射；重名加 server 前缀；每次调用落 `mcp_calls` 审计 |
| 平台市场 MCP 工具 | 平台轨启用 | 暴露名 `platform__{能力}__{工具}`；按用户 token；周期刷新 |
| 插件工具 | 插件启用 | 见下 |

### 3.1 MCP 服务器（`config/mcp_servers.json`）

- **默认启用**：`default`(数据库查询/邮件)、`dingtalk`、`rosiwit_cloud_remote`(设备控制)、`remote_terminal`(WS 终端, 300s)、`rosiwit_cloud_ticket`(BMS 工单)、`time`、`fetch`(SSRF 防护)。
- **默认禁用**（需显式开启并配置）：`mysql_query`、`filesystem`、`git`、`postgres`、`gerrit`、`gitlab`、`jira`。

### 3.2 插件工具（`src/plugins/`）

| 插件 | 工具 |
|---|---|
| dingtalk | `send_message_to_dingtalk`、`send_image_to_dingtalk` |
| feishu | `send_feishu_message`、`send_feishu_image` |
| kanban | `kanban_add`、`kanban_list`、`kanban_move`、`kanban_assign` |
| scheduler | `scheduler_create`、`scheduler_list`、`scheduler_update`、`scheduler_delete` |

## 四、可见性与渐进披露

- **核心（恒注入）**：所有 builtin + `skill` + `tool_search` + `whoami`。
- **远程（按需激活）**：MCP / 插件 / 市场工具；`search_tools`→`tool_search` 命中即激活，激活集按对话根（`RunContext.conversation_id`）持久化到 `session_meta.active_tools`。
- **模式**：`AGENT_TOOL_SEARCH`=auto（默认）/always/off；阈值 `AGENT_TOOL_SEARCH_THRESHOLD` 默认 40。

## 五、参数命名约定

- 计数上限统一 `limit`（`glob`/`grep`/`web_search`/`git log`/`tool_search`/`market_search`）。
- 字符上限统一 `max_chars`（`shell`/`web_fetch`）。
- 编辑统一 `old_string`/`new_string`（顶层与 `edits` 项一致）。
- 工具名：`tool_search`、`read_image`、`task`（旧名 `search_tools`/`view_image`/`subagent` 已废弃删除）。

## 六、权限与按 agent 作用域

- **有效权限 = RBAC(用户角色) ∩ agent 能力**，只能收紧不能放大。
- **agent 能力作用域**（`PROMPT.md`/`TEAM.md` frontmatter，见 `agent/capabilities.py`）：`tools`(白名单, 省略=全量)、`disallowedTools`(黑名单, 优先)、`mcpServers`(允许的 MCP server 名)、`skills`(技能白名单)、`permissionMode`(default/smart/auto/plan)、`platform_mcp`(平台轨开关)。核心工具恒可用，仅 `disallowedTools` 可移除。
- **双层强制**：暴露层 `Agent._collect_tool_defs` 四分支过滤 + 执行层 `execute_tool_safe` fail-closed。
- **权限模式**：`default`(写确认)/`smart`(仅危险确认)/`auto`(全放行)/`plan`(只读)。
- **写工具**（`security/permissions/rules.py:write_tools`）：`file`/`shell`/`edit`/`apply_patch`/`git`。

## 七、变更记录（2026 标准化）

- 直接改名（无别名）：`search_tools`→`tool_search`、`view_image`→`read_image`、`subagent`→`task`、`market_runtime`→`market_execute`（模块文件同步）。
- `market_delegate` 删除，能力并入 `market_execute`（`kind="agent"` + `capability="<专家名>"` 即委派专家）。
- `batch_edit` 删除，能力并入 `edit`（跨文件 `edits` 带 `file`）。
- `file` 删除 `preview`（与 `code_search` 重叠），精简为 read/write/append/delete/exists/list。
- 返回信封统一为 `success`（`market_*`/`read_image` 兼容保留 `ok`）。
- 参数命名统一（见 §五）。
