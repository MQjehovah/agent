# 用量视图设计（dashboard）

日期：2026-09-20
状态：已确认，待实施
关联：阶段二 · Part A（另见 rag 的 `docs/plans/2026-09-20-knowledge-authz-design.md`）

## 背景

桌面端「用量视图」从 roadmap 起就挂着未实现：`src/views/SettingsView.vue:163` 写着「用量视图将在后续版本接入」，`docs/design.md:176/186` 标为 P1/⏳。

调研发现两个硬约束：

1. **dashboard 目前无法访问 router 的用量接口。** 渲染层没有任何网络能力，只能通过主进程的 `upstream:request` 走 `agent/rag/market/router` 四个服务映射（`electron/main/upstream.ts:13-20`）；而 `router` 映射指向的是**网关** `routerUrl`(3100)，网关只有 `/v1/*` 推理路由，**没有任何用量查询接口**。用量接口全在 **admin**(3001) 上，且需要 JWT。
2. **router 的既有设计明确不做用户维度穿透**：`dashboard/docs/plans/2026-09-06-router-llm-collect-design.md:12,69` 规定 agent/rag 走应用级 key，router 只按「平台/应用」计量，「用户维度留在各平台自己用户体系」。

## 目标

在桌面端新增「用量」页，让员工看到**自己在 router 上可归属的**用量与配额。

## 非目标（YAGNI）

- **不改 router**（不复用 admin JWT 接口、不新增内部接口）。
- 不做时间趋势曲线、逐请求明细、账单/交易流水——现有内部接口拿不到，需要 router 改动。
- 不做 agent/rag 的应用级用量按人汇总——那需要三个系统都按人计量。
- 不做管理视角的公司/部门汇总。

## 数据口径与可得性

**口径**：仅该员工 SSO 开通的 `sso` key 上的用量（`/internal/sso/exchange` find-or-create，`router/admin/src/routes/sso.ts:12-13,76-79`）。dashboard 本地模式即用这个 key（`electron/main/kernel/ipc.ts:545-546`），因此其消耗天然归属到人。

**现有内部接口能取到的字段**（`router/admin/src/routes/internal.ts`）：

| 接口 | 鉴权 | 返回 |
|---|---|---|
| `POST /internal/keys/verify` `{apiKey, model?}` | `X-Internal-Secret` | `keyId, userId, rateLimit, dailyQuota, monthlyQuota, userBalance, todayTokens, monthTokens`；带 `model` 时另返回 `modelDailyQuota, modelMonthlyQuota, modelTodayTokens, modelMonthTokens` |
| `GET /internal/usage/daily/:keyId` | `X-Internal-Secret` | `{tokensIn, tokensOut, cost}` |
| `GET /internal/usage/monthly/:keyId` | `X-Internal-Secret` | `{tokensIn, tokensOut, cost}` |
| `POST /internal/keys/models` `{apiKey}` | `X-Internal-Secret` | `{models: string[]}` |

- `verify` 的 `todayTokens/monthTokens` 含 `cachedTokens`（`internal.ts:94-95`）。
- `verify` 只读且有 60s key 缓存（`internal.ts:34-41`），**无副作用**，因此「按模型分解」可用「对每个模型各调一次 verify」实现。
- **拿不到**：趋势、逐请求明细、`cachedTokens` 分项、交易/账单。

## 设计

### A1 主进程取数模块 `electron/main/usage.ts`（新增）

导出 `getUsageSummary(): Promise<UsageSummary>`：

1. 从 `getIdentity()` 取 `routerKey`；从 `getConfig()` 取 `routerAdminUrl`、`internalSecret`（`process.env.INTERNAL_SECRET` 优先）。任一缺失 → 抛明确中文错误。
2. `POST /internal/keys/models` → 模型列表。
3. `POST /internal/keys/verify`（不带 model）→ 总量、余额、配额。
4. `GET /internal/usage/daily/:keyId`、`GET /internal/usage/monthly/:keyId` → 入/出/花费。
5. 对模型列表的**前 10 个**模型各调一次 `verify {apiKey, model}` → 按模型用量。超出部分计入 `truncated: true`。
6. 组装 `UsageSummary` 返回。

错误分类（都转成可展示的中文）：
- 未配置 / 无 key：提示先完成 SSO 登录或检查设置页。
- 上游 401/403：key 失效或 Internal Secret 不匹配。
- 上游 5xx / 网络：网关管理端不可用。

实现要求：取数组合逻辑与网络调用分离（依赖注入 `fetchJson`），以便单测。

### A2 IPC

- `electron/main/index.ts` 注册 `ipcMain.handle('usage:get', ...)`。
- `electron/preload/index.ts` 的 `INVOKE_CHANNELS` 白名单加入 `'usage:get'`。

### A3 渲染层

- `src/api/types.ts`：新增 `UsageSummary`（含 `today/month` 的 `{tokensIn, tokensOut, tokens, cost}`、`balance`、`quota: {daily, monthly}`、`rateLimit`、`models: [{name, today, month, dailyQuota, monthlyQuota}]`、`truncated`）。
- `src/views/UsageView.vue`（新增），照 `KnowledgeView.vue` 的 loading/empty/error 模式：
  - 卡片：今日 tokens（入/出）+ 花费、本月 tokens + 花费、余额、限流。
  - 配额进度条：今日 tokens / 日配额、本月 tokens / 月配额（配额为 0 视为不限，不显示进度）。
  - ECharts：各模型「今日 vs 本月」tokens 柱状图；本月各模型 tokens 占比饼图。
  - 顶部刷新按钮；错误态给出可操作提示（未登录/未配置/上游不可用）。
- 路由：`src/router/index.ts` 加 `/usage`；侧栏 `src/layouts/WorkbenchLayout.vue` 的 `navMain` 加「用量」+ 图标。
- 依赖：新增 `echarts@^5.5.0`、`vue-echarts@^6.6.0`（与 `router/web` 版本一致）。
- 文案：`src/views/SettingsView.vue:163` 去掉「用量视图将在后续版本接入」；`docs/design.md` 路线图把用量视图从 ⏳ 改为已实现。

### A4 测试

- `usage.ts` 的组合逻辑（取数顺序、模型上限、错误映射）用 `node:test` + 注入的假 `fetchJson` 覆盖：正常、verify 401、缺配置、模型数超上限。
- 现有 169 个测试保持通过；`npm run typecheck` 与 `npm run build` 通过。

## 验收标准

1. 已 SSO 登录且配置齐全时，`/usage` 页正确显示今日/本月 tokens 与花费、余额、配额进度、限流、按模型柱状图与饼图。
2. 未配置 `routerAdminUrl`/`internalSecret` 或无 routerKey 时，页面显示明确中文提示而非空白或崩溃。
3. Internal Secret 错误（上游 403）时提示「网关管理端拒绝访问」。
4. 模型数超过 10 时只分解前 10 个并提示已截断。
5. 现有测试与构建全绿。

## 风险

- **数据说服力**：页面只反映 dashboard 本地模式的用量，员工若主要在 agent/rag 上消耗，会看到「用量很低」。必须在页面上明确标注口径，避免误解为「我全平台用量」。
- **按模型分解的请求放大**：模型数 × 2 次聚合查询；已用上限 10 约束。
- `routerAdminUrl` 默认 `http://192.168.31.34:3001` 是硬编码默认值（`electron/main/store.ts:33`），生产需在设置页/环境变量确认为实际地址。

## 实施结果（2026-09-20）

实施计划：`docs/plans/2026-09-20-usage-view.md`。提交：`a2dd3d4`（依赖与类型）、`416f368`（取数模块）、`72b9fd4`（Content-Type 与可测工厂）、`cc5f1b5`（IPC）、`6c9590b`（页面与路由）、`b3c7886`（文案与文档）。

| 验收标准 | 结果 | 证据 |
|---|---|---|
| 1. 配置齐全时展示今日/本月 tokens 与花费、余额、配额、限流、柱状图与饼图 | 部分验证 | 取数组合逻辑由单测覆盖；UI 由 `npm run build` + `vue-tsc` 通过；端到端展示需真实 router admin + 桌面端环境，未在本机执行 |
| 2. 缺配置/无 routerKey 时给出中文提示 | ✅ | `electron/main/usage.ts:115-122` 四条中文错误；单测覆盖 401 分支 |
| 3. Internal Secret 错误（上游 403）提示「网关管理端拒绝访问」 | ✅ | `usage.ts:31`；评审用本地 stub + 真实 `getUsageSummary` 实测得到该中文消息与 `status:401` |
| 4. 模型数 > 10 只分解前 10 个并提示截断 | ✅ | 单测 `模型数超过上限时只分解前 N 个并标记 truncated`；页面显示「仅展示前 N 个模型」 |
| 5. 现有测试与构建全绿 | ✅ | `npm test` 174/174、`npm run typecheck` exit 0、`npm run build` 成功 |

评审期间发现并修复的真实缺陷：

- **POST 缺少 `Content-Type: application/json`**：Node `fetch` 会把字符串 body 默认成 `text/plain`，Fastify 便不再 JSON 解析，`req.body.apiKey` 为 `undefined`，**所有** router admin 调用都会失败。已修复并加了请求形状断言（`makeAdminFetch` 注入 `fetchImpl`），且用「去掉该头则测试失败」的反向验证确认断言有效。
- 上游错误映射原先只覆盖 401/403/5xx，现覆盖所有 `>=400`，避免 404 之类以英文原文出现在中文界面。

已知未覆盖：桌面端真机端到端（验收标准 1）与真实 router admin 联调，需在有环境时人工确认。
