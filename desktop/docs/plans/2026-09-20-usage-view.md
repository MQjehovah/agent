# 用量视图 实施计划（dashboard）

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在桌面端新增「用量」页，展示该员工在 router 上可归属的用量、配额与按模型分解。

**Architecture:** 渲染层零网络；新增主进程模块 `usage.ts` 用 `X-Internal-Secret` 调 router admin 的**现有**内部接口（零 router 改动），组合成一个 `UsageSummary`，经新 IPC `usage:get` 暴露给渲染层。页面用 Element Plus + ECharts 展示。

**Tech Stack:** Electron + electron-vite + Vue 3 + TypeScript + Element Plus + Pinia + ECharts/vue-echarts；测试用 Node 内置 test runner + tsx。

**设计依据：** `dashboard/docs/plans/2026-09-20-usage-view-design.md`

---

## 前置约束（实现时不要越界）

- **不改 router 仓库**。
- 不使用 admin JWT 接口 `/api/usage/*`（SSO 用户拿不到 JWT）。
- 只读现有内部接口：`POST /internal/keys/verify`、`GET /internal/usage/daily/:keyId`、`GET /internal/usage/monthly/:keyId`、`POST /internal/keys/models`。
- 全程 `X-Internal-Secret` 头，取自 `process.env.INTERNAL_SECRET ?? getConfig().internalSecret`。
- 渲染层不得直接发网络请求——只能经 IPC。

样式约定：2 空格缩进、无分号、单引号、中文注释与中文 UI 文案、英文标识符。

---

## Task 1: 依赖与类型

**Files:**
- Modify: `dashboard/package.json`
- Modify: `dashboard/src/api/types.ts`

**Step 1: 添加依赖**

在 `package.json` 的 `dependencies` 中加入（与 `router/web` 保持一致的版本）：

```json
"echarts": "^5.5.0",
"vue-echarts": "^6.6.0"
```

Run: `npm install`（在 `dashboard/` 下）

**Step 2: 增加类型**

在 `src/api/types.ts` 末尾追加：

```ts
export interface UsageBucket {
  tokensIn: number
  tokensOut: number
  tokens: number
  cost: number
}

export interface UsageModelRow {
  name: string
  today: UsageBucket
  month: UsageBucket
  dailyQuota: number
  monthlyQuota: number
}

export interface UsageSummary {
  balance: number
  rateLimit: number
  quota: { daily: number; monthly: number }
  today: UsageBucket
  month: UsageBucket
  models: UsageModelRow[]
  /** 模型数超过分解上限时为 true */
  truncated: boolean
  fetchedAt: string
}
```

**Step 3: 类型检查**

Run: `npm run typecheck`
Expected: 通过（新类型未被引用，不会有错）。

**Step 4: 提交**

```bash
git add package.json package-lock.json src/api/types.ts
git commit -m "chore(dashboard): 引入 ECharts 并定义用量类型"
```

---

## Task 2: 主进程取数模块 `usage.ts`

**Files:**
- Create: `dashboard/electron/main/usage.ts`
- Test: `dashboard/test/kernel/usage.test.ts`

**Step 1: 先写失败测试**

Create `test/kernel/usage.test.ts`（参照 `test/kernel/` 下既有测试的 import 与断言风格）：

```ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildUsageSummary, MAX_MODEL_BREAKDOWN, type UsageDeps } from '../../electron/main/usage'

function makeDeps(overrides: Partial<UsageDeps> = {}): UsageDeps {
  const calls: string[] = []
  const deps: UsageDeps = {
    calls,
    async fetchJson(path: string, init?: { method?: string; body?: unknown }) {
      calls.push(path)
      if (path === '/internal/keys/models') return { models: ['m1', 'm2'] }
      if (path.startsWith('/internal/keys/verify')) {
        const body = (init?.body ?? {}) as { model?: string }
        if (!body.model) {
          return { keyId: 7, rateLimit: 60, dailyQuota: 100000, monthlyQuota: 3000000, userBalance: 12.5, todayTokens: 150, monthTokens: 900 }
        }
        return { modelTodayTokens: body.model === 'm1' ? 100 : 50, modelMonthTokens: body.model === 'm1' ? 600 : 300, modelDailyQuota: 0, modelMonthlyQuota: 0 }
      }
      if (path === '/internal/usage/daily/7') return { tokensIn: 100, tokensOut: 50, cost: 1.25 }
      if (path === '/internal/usage/monthly/7') return { tokensIn: 600, tokensOut: 300, cost: 7.5 }
      throw new Error(`unexpected path ${path}`)
    },
    ...overrides
  }
  return deps
}

test('组合出今日/本月/余额/配额与按模型分解', async () => {
  const summary = await buildUsageSummary(makeDeps())
  assert.equal(summary.balance, 12.5)
  assert.equal(summary.rateLimit, 60)
  assert.deepEqual(summary.quota, { daily: 100000, monthly: 3000000 })
  assert.deepEqual(summary.today, { tokensIn: 100, tokensOut: 50, tokens: 150, cost: 1.25 })
  assert.deepEqual(summary.month, { tokensIn: 600, tokensOut: 300, tokens: 900, cost: 7.5 })
  assert.deepEqual(summary.models.map((m) => m.name), ['m1', 'm2'])
  assert.equal(summary.models[0].today.tokens, 100)
  assert.equal(summary.models[1].month.tokens, 300)
  assert.equal(summary.truncated, false)
  assert.ok(summary.fetchedAt)
})

test('模型数超过上限时只分解前 N 个并标记 truncated', async () => {
  const many = Array.from({ length: MAX_MODEL_BREAKDOWN + 3 }, (_, i) => `m${i}`)
  const deps = makeDeps()
  const original = deps.fetchJson
  deps.fetchJson = async (path, init) => {
    if (path === '/internal/keys/models') return { models: many }
    return original(path, init)
  }
  const summary = await buildUsageSummary(deps)
  assert.equal(summary.models.length, MAX_MODEL_BREAKDOWN)
  assert.equal(summary.truncated, true)
})

test('上游 401 抛出可读中文错误', async () => {
  const deps = makeDeps({
    async fetchJson() {
      const err = new Error('upstream') as Error & { status?: number }
      err.status = 401
      throw err
    }
  })
  await assert.rejects(() => buildUsageSummary(deps), /密钥|凭证|401/)
})
```

**Step 2: 运行确认失败**

Run: `npm test`
Expected: FAIL —— 找不到 `buildUsageSummary` / `MAX_MODEL_BREAKDOWN`。

**Step 3: 实现 `electron/main/usage.ts`**

```ts
import { getConfig } from './store'
import { getIdentity } from './identity'
import type { UsageBucket, UsageModelRow, UsageSummary } from '../../src/api/types'

/** 按模型分解的上限:每个模型要 2 次聚合查询,避免请求放大 */
export const MAX_MODEL_BREAKDOWN = 10

export interface UsageDeps {
  fetchJson: (path: string, init?: { method?: string; body?: unknown }) => Promise<Record<string, unknown>>
  calls?: string[]
}

function num(v: unknown): number {
  const n = typeof v === 'string' ? Number(v) : (v as number)
  return Number.isFinite(n) ? n : 0
}

function bucket(tokensIn: unknown, tokensOut: unknown, cost: unknown, totalTokens?: unknown): UsageBucket {
  const tin = num(tokensIn)
  const tout = num(tokensOut)
  return {
    tokensIn: tin,
    tokensOut: tout,
    tokens: totalTokens === undefined ? tin + tout : num(totalTokens),
    cost: num(cost)
  }
}

/** 纯组合逻辑:不读全局状态,便于单测 */
export async function buildUsageSummary(deps: UsageDeps): Promise<UsageSummary> {
  const { fetchJson } = deps
  const models = ((await fetchJson('/internal/keys/models', { method: 'POST' })).models ?? []) as string[]
  const verify = await fetchJson('/internal/keys/verify', { method: 'POST' })
  const keyId = num(verify.keyId)
  if (!keyId) throw new Error('网关返回的 keyId 无效,无法查询用量')

  const daily = await fetchJson(`/internal/usage/daily/${keyId}`)
  const monthly = await fetchJson(`/internal/usage/monthly/${keyId}`)

  const picked = models.slice(0, MAX_MODEL_BREAKDOWN)
  const rows: UsageModelRow[] = []
  for (const name of picked) {
    const r = await fetchJson('/internal/keys/verify', { method: 'POST', body: { model: name } })
    rows.push({
      name,
      today: bucket(0, 0, 0, num(r.modelTodayTokens)),
      month: bucket(0, 0, 0, num(r.modelMonthTokens)),
      dailyQuota: num(r.modelDailyQuota),
      monthlyQuota: num(r.modelMonthlyQuota)
    })
  }

  return {
    balance: num(verify.userBalance),
    rateLimit: num(verify.rateLimit),
    quota: { daily: num(verify.dailyQuota), monthly: num(verify.monthlyQuota) },
    today: bucket(daily.tokensIn, daily.tokensOut, daily.cost, num(verify.todayTokens)),
    month: bucket(monthly.tokensIn, monthly.tokensOut, monthly.cost, num(verify.monthTokens)),
    models: rows,
    truncated: models.length > MAX_MODEL_BREAKDOWN,
    fetchedAt: new Date().toISOString()
  }
}

/** 组装真实依赖并取数(供 IPC 调用) */
export async function getUsageSummary(): Promise<UsageSummary> {
  const identity = getIdentity()
  if (!identity) throw new Error('请先完成企业 SSO 登录')
  if (!identity.routerKey) throw new Error('尚未获取算力网关凭证,请在设置页检查或重新登录')

  const cfg = getConfig()
  const base = (cfg.routerAdminUrl ?? '').replace(/\/+$/, '')
  const secret = process.env.INTERNAL_SECRET ?? cfg.internalSecret
  if (!base) throw new Error('未配置路由管理端地址(设置页 → Router Admin)')
  if (!secret) throw new Error('未配置 Internal Secret(设置页 → Internal Secret)')

  const fetchJson = async (path: string, init?: { method?: string; body?: unknown }) => {
    const res = await fetch(`${base}${path}`, {
      method: init?.method ?? 'GET',
      headers: { 'Content-Type': 'application/json', 'X-Internal-Secret': secret },
      body: init?.body === undefined ? undefined : JSON.stringify({ apiKey: identity.routerKey, ...(init.body as object) })
    })
    if (res.status === 401 || res.status === 403) {
      throw Object.assign(new Error('网关管理端拒绝访问:凭证失效或 Internal Secret 不匹配'), { status: res.status })
    }
    if (!res.ok) {
      throw Object.assign(new Error(`网关管理端返回 HTTP ${res.status}`), { status: res.status })
    }
    return (await res.json()) as Record<string, unknown>
  }

  return buildUsageSummary({ fetchJson })
}
```

> 注意 `GET /internal/usage/daily/:keyId` 与 `/monthly` 是 GET，不带 body；`verify` 与 `models` 是 POST，需要 `apiKey`。实现时按上表对齐，不要给 GET 加 body。

**Step 4: 运行确认通过**

Run: `npm test`
Expected: 新增 3 个测试全部 PASS，既有 169 个仍通过。

**Step 5: 提交**

```bash
git add electron/main/usage.ts test/kernel/usage.test.ts
git commit -m "feat(dashboard): 主进程用量取数与组合逻辑"
```

---

## Task 3: IPC 接线

**Files:**
- Modify: `dashboard/electron/main/index.ts`
- Modify: `dashboard/electron/preload/index.ts`

**Step 1: 主进程注册 handler**

在 `electron/main/index.ts` 中，与其他 `ipcMain.handle` 并列处加入（`import` 顶部加入 `getUsageSummary`）：

```ts
ipcMain.handle('usage:get', async () => {
  return getUsageSummary()
})
```

参考该文件中既有 handler 的错误返回约定（若既有 handler 用 try/catch 包成 `{ ok, error }`，则保持一致；先读该文件确认）。

**Step 2: preload 白名单**

在 `electron/preload/index.ts` 的 `INVOKE_CHANNELS`（或等价白名单数组）加入 `'usage:get'`。

**Step 3: 验证**

Run: `npm run typecheck; npm run build`
Expected: 均通过。

**Step 4: 提交**

```bash
git add electron/main/index.ts electron/preload/index.ts
git commit -m "feat(dashboard): 暴露 usage:get IPC"
```

---

## Task 4: 用量页面与路由

**Files:**
- Create: `dashboard/src/views/UsageView.vue`
- Modify: `dashboard/src/router/index.ts`
- Modify: `dashboard/src/layouts/WorkbenchLayout.vue`

**Step 1: 写页面**

Create `src/views/UsageView.vue`，参考 `src/views/KnowledgeView.vue` 的 loading / empty / error 结构：

- `onMounted`：`if (!settings.hasUser) return`，然后 `load()`。
- `load()`：`const summary = await window.desktop.invoke<UsageSummary>('usage:get')`；`catch (e)` 时把 `(e as Error).message` 显示到 `error`。
- 顶部 `view-header`：标题「用量」+ 刷新按钮（`el-button` 带 `Refresh` 图标）。
- 口径说明：醒目地写一行「仅统计桌面端（算力网关）用量；agent/知识库的用量不计入」。
- 卡片区（`el-row`/`el-col` + `el-card` 或 `el-statistic`）：
  - 今日 tokens（入 / 出）+ 花费
  - 本月 tokens（入 / 出）+ 花费
  - 余额
  - 限流（次/分）
- 配额进度条：`el-progress`。今日 tokens / `quota.daily`、本月 tokens / `quota.monthly`；**配额为 0 时显示「不限」且不渲染进度条**。
- 图表（ECharts，用 `vue-echarts` 的 `VChart`）：
  - 柱状图：x 轴为模型名，两个系列「今日 tokens / 本月 tokens」。
  - 饼图：本月各模型 tokens 占比。
  - `truncated` 为 true 时在图表下方提示「仅展示前 N 个模型」。
- 空态：`models` 为空时只显示卡片与配额，图表区给 `el-empty`。
- 金额格式化为两位小数（如 `¥1.25`）。

**Step 2: 注册路由**

`src/router/index.ts` 的 `WorkbenchLayout` children 中加入（放在 `market` 之后）：

```ts
{ path: 'usage', name: 'usage', component: () => import('../views/UsageView.vue'), meta: { title: '用量' } },
```

**Step 3: 侧栏入口**

`src/layouts/WorkbenchLayout.vue` 的 `navMain` 数组加入：

```ts
{ path: '/usage', title: '用量', icon: TrendCharts },
```

并在该文件顶部的 `@element-plus/icons-vue` 导入中加入 `TrendCharts`。

**Step 4: 验证**

Run: `npm run typecheck; npm run build`
Expected: 通过（`vue-tsc` 类型错误会阻断构建）。

**Step 5: 提交**

```bash
git add src/views/UsageView.vue src/router/index.ts src/layouts/WorkbenchLayout.vue
git commit -m "feat(dashboard): 新增用量页面与导航入口"
```

---

## Task 5: 文案与文档收尾

**Files:**
- Modify: `dashboard/src/views/SettingsView.vue:163`
- Modify: `dashboard/docs/design.md`

**Step 1: 去掉过时提示**

把 `SettingsView.vue:163` 中的
`对话能力由 agent 执行引擎提供;知识检索 / 能力市场 / 用量视图将在后续版本接入。`
改为
`对话能力由 agent 执行引擎提供;知识检索 / 能力市场 / 用量视图见左侧导航。`

**Step 2: 更新路线图**

`docs/design.md` 中把用量视图相关行（`:70`、`:176`、`:186`）的状态从 `⏳ 规划中(原 P1 内容)` / `P1 用量视图` 改为「已实现」，并注明数据口径为「仅 router 可归属用量，零 router 改动」。同时把 `README.md:71` 的路线图描述同步。

**Step 3: 提交**

```bash
git add src/views/SettingsView.vue docs/design.md README.md
git commit -m "docs(dashboard): 用量视图状态与口径说明"
```

---

## Task 6: 端到端验收

**Step 1: 全量验证**

Run（在 `dashboard/` 下）:
```
npm test
npm run typecheck
npm run build
```
Expected: 测试全绿（169 + 新增 3 = 172）、类型检查通过、构建成功。

**Step 2: 手动验收清单**

逐条核对设计文档《验收标准》1–5，并把结果记录到 `docs/plans/2026-09-20-usage-view-design.md` 末尾的「实施结果」小节：

1. 配置齐全时可看到今日/本月 tokens 与花费、余额、配额、限流、柱状图与饼图。
2. 未配置 `routerAdminUrl`/`internalSecret` 或缺 routerKey 时给出中文提示而非空白/崩溃。
3. Internal Secret 错误时提示「网关管理端拒绝访问」。
4. 模型数 > 10 时只分解前 10 个并提示截断。
5. 现有测试与构建全绿。

**Step 3: 提交**

```bash
git add docs/plans/2026-09-20-usage-view-design.md
git commit -m "docs(dashboard): 用量视图实施结果"
```

---

## 已知限制（不要试图在本计划内解决）

- 无时间趋势、无逐请求明细、无账单/交易——现有内部接口不提供。
- 只覆盖 dashboard 本地模式；agent/rag 的应用级用量不计入（页面已明示口径）。
- `verify` 对 key 有 60s 缓存，因此刚产生的用量可能延迟显示。
