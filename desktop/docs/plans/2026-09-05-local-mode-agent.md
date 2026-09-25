# 本地模式 Agent 内核实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在 Electron 主进程实现 TypeScript agent 内核（agent 循环 + 内置工具 + MCP + 技能 + 会话持久化 + 权限确认），工作台新增「本地模式」，与「零号员工」按会话并存。

**Architecture:** 内核全部位于 `electron/main/kernel/`，经 IPC 与渲染层通信；LLM 经 router `/v1/chat/completions`（Bearer 员工 apikey，SSE）；事件协议 `{type:'token'|'tool_call'|'tool_result'|'permission_request'|'done'|'error'}` 与零号员工 ChatStreamEvent 词汇对齐。设计文档：`docs/plans/2026-09-05-local-mode-agent-design.md`。

**Tech Stack:** TypeScript (Electron main)、`@modelcontextprotocol/sdk`（MCP）、`node:test` + `tsx`（单测）、无重框架（工具参数用内联 JSON Schema）。

**前置说明（给零上下文工程师）：**
- 项目根：`E:\workspace_ai\dashboard`（git 仓库，命令均在此执行）。
- 凭据：router apikey 已在 `electron/main/identity.ts` 的 `getIdentity().routerKey`；router 地址在 `getConfig().routerUrl`。
- 现有事件通道模式参考 `electron/main/upstream.ts`（streamId + `webContents.send`）。
- 测试命令：`npm test`（Task 0 会加上）。每个 Task 结束 `npm run typecheck:node` 必须通过再提交。

---

### Task 0: 测试基建

**Files:**
- Modify: `package.json`（scripts + devDependencies）
- Create: `test/kernel/.gitkeep`

**Step 1:** `npm install -D tsx`；`package.json` scripts 加 `"test": "tsx --test test/**/*.test.ts"`。

**Step 2:** 验证：`npm test`（0 用例通过即成功）。

**Step 3:** Commit: `chore: add tsx test runner`

---

### Task 1: 内核类型与工具注册表

**Files:**
- Create: `electron/main/kernel/types.ts`
- Create: `electron/main/kernel/registry.ts`
- Test: `test/kernel/registry.test.ts`

**Step 1: 写失败测试**（`test/kernel/registry.test.ts`）

```ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRegistry } from '../../electron/main/kernel/registry'

const demo = {
  name: 'demo', description: 'd', kind: 'read' as const,
  parameters: { type: 'object', properties: {} },
  execute: async () => ({ ok: true, output: 'ok' })
}

test('registry: register/get/list roundtrip', () => {
  const r = createRegistry()
  r.register(demo)
  assert.equal(r.get('demo')?.name, 'demo')
  assert.deepEqual(r.list().map(t => t.name), ['demo'])
})

test('registry: toOpenAiTools shapes function schema', () => {
  const r = createRegistry(); r.register(demo)
  assert.deepEqual(r.toOpenAiTools(), [{
    type: 'function',
    function: { name: 'demo', description: 'd', parameters: { type: 'object', properties: {} } }
  }])
})

test('registry: duplicate register throws', () => {
  const r = createRegistry(); r.register(demo)
  assert.throws(() => r.register(demo))
})
```

**Step 2:** `npm test` → FAIL（模块不存在）。

**Step 3: 实现** `types.ts`：

```ts
export interface ToolContext { workspace: string; sessionId: string }
export interface ToolResult { ok: boolean; output: string }
export interface ToolDefinition {
  name: string
  description: string
  /** 'read' 自动放行;'write' 需权限确认 */
  kind: 'read' | 'write'
  parameters: Record<string, unknown>
  execute(args: Record<string, unknown>, ctx: ToolContext): Promise<ToolResult>
}
export interface ToolCall { id: string; name: string; arguments: string }
export interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string
  toolCalls?: ToolCall[]
  toolCallId?: string
  name?: string
}
export type AgentEvent =
  | { type: 'token'; text: string }
  | { type: 'tool_call'; name: string; args: string }
  | { type: 'tool_result'; name: string; output: string; ok: boolean }
  | { type: 'permission_request'; requestId: string; tool: string; summary: string }
  | { type: 'done' }
  | { type: 'error'; message: string }
```

`registry.ts`：

```ts
import type { ToolDefinition } from './types'

export interface Registry {
  register(tool: ToolDefinition): void
  get(name: string): ToolDefinition | undefined
  list(): ToolDefinition[]
  toOpenAiTools(): Array<{ type: 'function'; function: { name: string; description: string; parameters: Record<string, unknown> } }>
}

export function createRegistry(): Registry {
  const tools = new Map<string, ToolDefinition>()
  return {
    register(tool) {
      if (tools.has(tool.name)) throw new Error(`tool already registered: ${tool.name}`)
      tools.set(tool.name, tool)
    },
    get: (name) => tools.get(name),
    list: () => [...tools.values()],
    toOpenAiTools: () => [...tools.values()].map(t => ({
      type: 'function' as const,
      function: { name: t.name, description: t.description, parameters: t.parameters }
    }))
  }
}
```

**Step 4:** `npm test` → PASS；`npm run typecheck:node` → 通过。

**Step 5:** Commit: `feat(kernel): tool types and registry`

---

### Task 2: 工作区路径安全

**Files:**
- Create: `electron/main/kernel/pathsafe.ts`
- Test: `test/kernel/pathsafe.test.ts`

**Step 1: 失败测试**

```ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { resolveWithin, isWithin } from '../../electron/main/kernel/pathsafe'

test('pathsafe: relative path resolves inside workspace', () => {
  assert.ok(isWithin('C:\\ws', resolveWithin('C:\\ws', 'a\\b.txt')))
})

test('pathsafe: dotdot escape rejected', () => {
  assert.throws(() => resolveWithin('C:\\ws', '..\\escape.txt'))
})

test('pathsafe: absolute path inside allowed, sibling rejected', () => {
  assert.ok(isWithin('C:\\ws', resolveWithin('C:\\ws', 'C:\\ws\\x.txt')))
  assert.throws(() => resolveWithin('C:\\ws', 'C:\\ws2\\x.txt'))
})
```

**Step 2:** FAIL。

**Step 3: 实现** `pathsafe.ts`（用 `path.resolve` + 大小写不敏感比较，Windows 盘符）：

```ts
import { resolve, sep } from 'node:path'

function norm(p: string): string {
  return resolve(p).toLowerCase().replace(/[\\/]+$/, '')
}

export function isWithin(workspace: string, target: string): boolean {
  const w = norm(workspace)
  const t = norm(target)
  return t === w || t.startsWith(w + sep.toLowerCase()) || t.startsWith(w + '\\')
}

export function resolveWithin(workspace: string, target: string): string {
  const abs = resolve(workspace, target)
  if (!isWithin(workspace, abs)) throw new Error(`路径越界: ${target} 不在工作区内`)
  return abs
}
```

**Step 4:** PASS + typecheck。**Step 5:** Commit: `feat(kernel): workspace path safety`

---

### Task 3: 内置工具

**Files:**
- Create: `electron/main/kernel/tools.ts`
- Test: `test/kernel/tools.test.ts`（覆盖 file.edit 三态：命中替换/未命中报错/all 替换；file.write 越界拒绝；glob/grep 冒烟）

**Step 1: 失败测试**（要点）

```ts
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileTools } from '../../electron/main/kernel/tools'

const ws = mkdtempSync(join(tmpdir(), 'ktest-'))
const ctx = { workspace: ws, sessionId: 's1' }

test('file.write/read roundtrip', async () => {
  const [w] = fileTools.filter(t => t.name === 'file.write')
  const [r] = fileTools.filter(t => t.name === 'file.read')
  await w.execute({ path: 'a.txt', content: 'hello' }, ctx)
  const out = await r.execute({ path: 'a.txt' }, ctx)
  assert.equal(out.output, 'hello')
})

test('file.edit replaces exact match', async () => {
  writeFileSync(join(ws, 'b.txt'), 'foo bar')
  const [e] = fileTools.filter(t => t.name === 'file.edit')
  const res = await e.execute({ path: 'b.txt', old: 'bar', new: 'baz' }, ctx)
  assert.ok(res.ok)
  assert.equal(readFileSync(join(ws, 'b.txt'), 'utf-8'), 'foo baz')
})

test('file.edit without match fails', async () => {
  const [e] = fileTools.filter(t => t.name === 'file.edit')
  const res = await e.execute({ path: 'b.txt', old: 'nope', new: 'x' }, ctx)
  assert.ok(!res.ok)
})

test('file.write outside workspace rejected', async () => {
  const [w] = fileTools.filter(t => t.name === 'file.write')
  const res = await w.execute({ path: join(ws, '..', 'evil.txt'), content: 'x' }, ctx)
  assert.ok(!res.ok)
})
```

（glob/grep 用临时目录造 2 个文件断言命中；terminal 测试 `echo ok` 断言 output 含 `ok`。）

**Step 2:** FAIL。**Step 3: 实现** `tools.ts`：每个工具一个对象字面量组成 `fileTools` 数组；`file.*` 一律先 `resolveWithin`；`terminal` 用 `child_process.exec`（`{ cwd: ctx.workspace, timeout: timeoutSec*1000, maxBuffer: 1MB }`，Windows shell 固定 `cmd /c`）；`glob`/`grep` 用 `node:fs` 递归 + 简单模式匹配（不引依赖：glob 支持 `**`/`*` 转正则；grep 逐文件按行 includes/正则）。工具 kind：read/glob/grep='read'，write/edit/terminal='write'。

**Step 4:** PASS + typecheck。**Step 5:** Commit: `feat(kernel): builtin file/terminal/glob/grep tools`

---

### Task 4: 会话持久化与上下文

**Files:**
- Create: `electron/main/kernel/session.ts`
- Test: `test/kernel/session.test.ts`（用 `GATEWAY_DATA_DIR` 指向临时目录）

**Step 1: 失败测试**（要点）：create→list→appendMessage→getMessages 往返；delete 后 list 为空；`buildContext(id, {maxChars})` 超限丢中间消息但保留 system 与最近两条。

**Step 2:** FAIL。

**Step 3: 实现** `session.ts`：目录 `join(process.env.GATEWAY_DATA_DIR!, 'localagent', 'sessions')`；每会话 `<id>.meta.json` + `<id>.messages.jsonl`；接口：

```ts
export interface LocalSession {
  id: string; mode: 'local'; title: string; model: string
  workspace: string; systemPrompt?: string; createdAt: number; updatedAt: number
}
listSessions(): LocalSession[]
createSession(input: { title?: string; model: string; workspace: string; systemPrompt?: string }): LocalSession
deleteSession(id: string): void
getSession(id: string): LocalSession | null
appendMessage(id: string, msg: StoredMessage): void
getMessages(id: string): StoredMessage[]
buildContext(id: string, maxChars: number): ChatMessage[]  // system 首条由 loop 注入,这里只滑窗历史
```

**Step 4:** PASS + typecheck。**Step 5:** Commit: `feat(kernel): local session persistence and context window`

---

### Task 5: 权限管理

**Files:**
- Create: `electron/main/kernel/permissions.ts`
- Test: `test/kernel/permissions.test.ts`

**Step 1: 失败测试**（要点）：`kind==='read'` 直接 true；`write` 工具未确认时挂起（promise 未决），`respond(requestId,'allow')` 后 resolve true；`deny` → false；`allow_session` → 同会话同工具后续直接 true。

```ts
export interface PermissionGateway {
  ask(sessionId: string, tool: string, summary: string): Promise<boolean>
  respond(requestId: string, decision: 'allow' | 'deny' | 'allow_session'): void
}
export function createPermissions(notify: (req: { requestId: string; sessionId: string; tool: string; summary: string }) => void): PermissionGateway
```

**Step 2:** FAIL。**Step 3:** 实现：Map<requestId, {resolve, sessionId, tool}> + Set<`${sessionId}:${tool}`> 会话级放行；ask 时 read 直接 true——注意：read/write 判定放 registry/loop 侧，permissions 只管确认，保持单一职责（ask 即需确认）。

**Step 4:** PASS + typecheck。**Step 5:** Commit: `feat(kernel): permission gateway with session grants`

---

### Task 6: 技能加载器

**Files:**
- Create: `electron/main/kernel/skills.ts`
- Test: `test/kernel/skills.test.ts`

SKILL.md 格式（与 agent/ 生态同构）：

```markdown
---
name: commit-helper
description: 按约定式提交规范生成 commit message
---
正文指令……
```

**Step 1: 失败测试**：临时 skills 目录两个技能；`listSkills()` 返回 name/description；`skillBody('commit-helper')` 返回正文；坏 front-matter 跳过不抛。

**Step 2:** FAIL。**Step 3:** 实现 `skills.ts`：`skillsDir()` = `GATEWAY_DATA_DIR/localagent/skills`；front-matter 用 `---` 分隔 + 行级 `key: value` 解析（不引依赖）；`systemPromptAddendum()` 拼接「可用技能列表」段落。

**Step 4:** PASS + typecheck。**Step 5:** Commit: `feat(kernel): SKILL.md skill loader`

---

### Task 7: MCP 客户端

**Files:**
- Create: `electron/main/kernel/mcp.ts`
- Test: `test/kernel/mcp.test.ts`（仅配置解析/命名合并的纯逻辑单测；真实 MCP 连接留冒烟）

**说明:** `npm install @modelcontextprotocol/sdk`。配置 `GATEWAY_DATA_DIR/localagent/mcp.json`：

```json
{ "servers": [{ "name": "filesystem", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\ws"] }] }
```

**接口：**

```ts
parseMcpConfig(raw: string): { servers: McpServerConfig[] }   // 纯函数,单测
async function connectMcpServers(registry: Registry, opts: { workspace: string; onStatus?: (s: string) => void }): Promise<void>
// stdio server 用 SDK StdioClientTransport 拉起;listTools 后包装为 ToolDefinition 注册:
//   name = `mcp__${server}__${tool}`, kind='write'(默认需确认), parameters=tool.inputSchema
```

**步骤:** 失败测试 → 实现 parseMcpConfig（校验 name/command 或 url）→ PASS → `connectMcpServers` 仅类型检查通过（真实连接冒烟放 Task 11）。Commit: `feat(kernel): mcp client config and registry merge`

---

### Task 8: Agent 循环

**Files:**
- Create: `electron/main/kernel/loop.ts`
- Test: `test/kernel/loop.test.ts`（SSE 帧解析 + 消息组装纯逻辑；完整循环本地冒烟）

**Step 1: 失败测试**：抽纯函数 `parseSseChoiceDelta(frame)` 与 `assembleToolCalls(deltas)`（把流式 tool_calls 增量按 index 拼成完整 ToolCall[]）单测；`buildSystemPrompt(base, addendum)` 断言拼接。

**Step 2:** FAIL。

**Step 3: 实现** `loop.ts` 核心：

```ts
export interface LoopDeps {
  apiKey: string; baseUrl: string           // router,如 http://127.0.0.1:3100
  registry: Registry
  permissions: PermissionGateway
  maxRounds?: number                        // 默认 25
}
export async function runAgentTurn(deps: LoopDeps, input: {
  sessionId: string; workspace: string; model: string
  history: ChatMessage[]                    // 已含 system
  userMessage: string
  emit: (e: AgentEvent) => void
  signal?: AbortSignal
}): Promise<void>
```

流程：
1. messages = [...history, {role:'user', content}]
2. fetch `${baseUrl}/v1/chat/completions`（stream:true, tools:registry.toOpenAiTools(), signal）
3. SSE 累积：token → emit token；tool_calls 增量拼装；`finish_reason==='tool_calls'` 时对每个 call：
   - emit tool_call
   - `tool.kind==='write'` → `permissions.ask()`（false 则 tool 结果为「用户拒绝」文本）
   - `execute(JSON.parse(arguments), {workspace, sessionId})`
   - emit tool_result；messages 追加 assistant(toolCalls) + tool 结果
   - 回到 2
4. finish_reason==='stop' → emit done；超 maxRounds → emit error
5. 非 200：读 body 错误 → emit error（401 特判文案「apikey 无效或已过期,请重新 SSO 登录」）
6. 每次 turn 结束后由调用方负责把新增消息写入 session.ts

**Step 4:** PASS + typecheck。**Step 5:** Commit: `feat(kernel): agent tool-calling loop over router SSE`

---

### Task 9: IPC 装配

**Files:**
- Create: `electron/main/kernel/ipc.ts`
- Modify: `electron/main/index.ts`（`registerKernelIpc()`；启动时 `process.env.GATEWAY_DATA_DIR` 已就位）
- Modify: `electron/main/store.ts`（AppConfig 增 `localModel: string`，默认 `glm-5.3-flash`）

**通道实现要点**（参考 upstream.ts 的 streamId+send 模式）：

- `localagent:sessions:list|create|delete`、`localagent:messages` → 直调 session.ts
- `localagent:workspace:pick` → `dialog.showOpenDialog({ properties:['openDirectory','createDirectory'] })`
- `localagent:chat` {sessionId, message}：校验 `getIdentity()?.routerKey`（无则 throw「请先完成企业 SSO 登录」）；createRegistry 注册内置工具 + `await connectMcpServers`（进程级缓存，已连接跳过）；返回 `{streamId}`；`runAgentTurn` 的 emit 桥接为 `event.sender.send('localagent:event', {streamId, ...e})`；turn 内每条新消息落盘 `appendMessage`
- `localagent:stop` {streamId} → AbortController map
- `localagent:permissions-respond` {requestId, decision} → permissions.respond

**步骤:** typecheck 通过 → 手动 `npm run build`。Commit: `feat(kernel): ipc wiring for local agent`

---

### Task 10: preload 与渲染层类型

**Files:**
- Modify: `electron/preload/index.ts`（白名单加 `localagent:*` 7 个通道；新增 `onLocalAgentEvent(cb)` 订阅 `localagent:event`，模式同 onUpstreamEvent）
- Modify: `src/env.d.ts`（LocalAgentEventPayload 类型 + bridge 方法）

**步骤:** typecheck:web 通过。Commit: `feat(preload): expose local agent ipc`

---

### Task 11: 渲染层——会话模式与聊天分流

**Files:**
- Modify: `src/stores/chat.ts`（session 增加 `mode:'agent'|'local'`；`send()` 按 mode 分流：local 走 `localagent:chat` + `onLocalAgentEvent`，事件映射到现有 UiMessage/tools 轨迹；新增权限弹窗状态 `pendingPermission` 与 `respondPermission(decision)`）
- Modify: `src/views/ChatView.vue`（新建会话处模式选择控件；本地模式隐藏「子代理」类提示；权限确认对话框 Element Plus `el-dialog`）
- Modify: `src/stores/sessions.ts` + `src/views/SessionsView.vue`（列表合并 agent+local，徽标「本地」；删除按 mode 分流）
- Modify: `src/stores/settings.ts` + `src/views/SettingsView.vue`（`localModel` 默认模型设置项）
- Modify: `src/api/types.ts`（AppConfig + localModel；LocalSession/AgentEvent 渲染层类型）

**要点:** 零号员工链路行为不变；本地会话标题「本地」徽标；工作区选择：新建本地会话时若无 workspace 先调 `localagent:workspace:pick`。

**验证:** `npm run typecheck` + `npm run build`。Commit: `feat(ui): dual-mode workbench with local agent sessions`

---

### Task 12: 端到端冒烟（手动）

**前置:** router 网关跑在 `routerUrl`（默认 3100）且 admin 已配 OIDC；dashboard `npm run dev`。

1. SSO 登录 → 设置页确认 Router Admin/Secret → 主进程日志无 key 交换降级警告
2. 新建「本地模式」会话 → 选工作区 → 输入「列出当前目录文件」→ 观察：token 流式输出、tool_call(glob/file.read) 轨迹、权限弹窗（允许后执行）
3. 「允许本会话不再询问」后同类工具不再弹窗
4. 重启应用 → 会话与消息恢复（身份加密落盘 + 会话 JSONL）
5. agent 对话发起 MCP：在 mcp.json 配一个 filesystem server → 新会话确认 `mcp__filesystem__*` 工具出现
6. 回归：零号员工会话对话、SessionsView 双列表、agent 模式不受影响

**提交:** 全部通过后 Commit: `test: local agent e2e smoke checklist`（仅文档如有更新）。

---

## 风险与备注

- router 必须透传 `tools`/`tool_calls` 字段（网关是 body 透传，理论无碍；Task 12 第 2 步即验证点）。
- Windows 终端工具固定 `cmd /c`，PowerShell 语义差异由提示词约束模型。
- MCP server 进程生命周期：进程级缓存 + app quit 时 close（Task 9 在 `app.on('will-quit')` 挂钩）。
