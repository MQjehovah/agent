# 本地模式 Agent 内核设计（Electron TS）

日期：2026-09-05
状态：已评审通过

## 背景与定位

dashboard（员工 AI 工作台，Electron + Vue）对标 Codex / WorkBuddy / ZCode：桌面壳 + 本地 agent 引擎 + 工具/MCP/技能生态。工作台提供两种模式，按会话选择：

- **零号员工**：公司共享 agent（`agent/` 项目部署实例），走 agent 接口访问 LLM——现状不动。
- **本地模式**：跑在 Electron 主进程的 TypeScript agent 内核，LLM 经 router 网关（员工 SSO 换取的 apikey），后续承接插件市场安装的 agent/工具/MCP/技能。

架构前提（已落地）：渲染层与主进程仅 IPC 通信、零本地 HTTP 端口；一切凭据（router apikey、agent JWT、OIDC token）只在主进程。

## 内核架构

```
主进程 electron/main/kernel/
├─ loop.ts        agent 循环:LLM tool-calling 迭代执行
├─ registry.ts    工具注册表:内置工具 + MCP 工具 + 市场安装(未来)
├─ tools/         file.read / file.write / file.edit / terminal / glob / grep
├─ mcp/           MCP 客户端(@modelcontextprotocol/sdk,stdio + SSE)
├─ skills/        SKILL.md 加载器
└─ session.ts     会话持久化(JSONL)+ 上下文滑窗
```

IPC 通道：`localagent:chat`（返回 streamId，事件经 `localagent:event` 推送）、`localagent:stop`、`localagent:sessions:list/create/delete`、`localagent:messages`、`localagent:permissions-respond`。

## Agent 循环

- 请求：router `/v1/chat/completions`，`Authorization: Bearer <员工 apikey>`，SSE 流式，携带 tools 定义（OpenAI function calling 格式）。
- 返回 tool_calls → 主进程执行 → 结果以 tool 角色消息回填 → 继续迭代；上限 25 轮。
- token 增量实时推送渲染层。

## 内置工具

`file.read` / `file.write` / `file.edit`（精确替换）/ `terminal`（cwd=工作区，输出截断 + 超时）/ `glob` / `grep`。全部以用户选定的工作区目录为根，拒绝越界路径。

## 权限模型

- 只读工具（read/glob/grep）自动放行。
- `file.write/edit`、`terminal` 默认弹窗确认：允许 / 拒绝 / 本会话不再询问。
- 工作区外路径一律要求确认。

## MCP

- 配置文件列出 MCP server（stdio: command/args；或 SSE URL）。
- 启动会话时拉起 server，工具并入注册表，命名 `mcp__<server>__<tool>`。
- 插件市场安装本质 = 向该配置写入条目（本期只留格式，不做安装流程）。

## 技能

- `skills/<name>/SKILL.md`：front-matter（name/description）+ 指令正文，与 agent/ 项目的 skill 格式同构。
- 描述注入 system prompt；`/技能名` 显式触发或模型按描述自动引用。

## 会话与上下文

- 会话字段：id、mode('local')、title、model、workspace、systemPrompt?、createdAt/updatedAt。
- 消息 JSONL 落盘 userData；上下文取最近消息，按字符数估算滑窗截断。

## 事件协议

`{type:'token'|'tool_call'|'tool_result'|'done'|'error'}`，与零号员工 ChatStreamEvent 词汇对齐，ChatView 工具轨迹 UI 复用。

## UI

- 新建会话选择模式；本地会话标题旁「本地」徽标。
- 权限确认弹窗；本地模式首次使用时选择工作区目录。
- 提示文案按模式区分（本地模式无子 agent 轨迹）。

## 模型与配置

- 默认模型设置项（默认 `glm-5.3-flash`），会话可覆盖。
- 主进程可凭 internalSecret 调 router admin `/internal/keys/models` 拉取该 key 可用模型列表，失败退化为手填。
- 无 apikey（未 SSO/交换失败）发消息 → 提示先完成企业 SSO；router 401 → 提示 key 失效重登；工具执行错误作为 tool_result 回传模型自行处理。

## 本期不做

子 agent、进程级沙箱隔离、插件市场安装流程、自动更新。
