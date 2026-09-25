# LLM / Embedding 收口 router 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** agent/rag 的 chat 与 rag 的 embedding 收口到 router（应用级 key），dashboard 本地模式维持按人不改。

**Architecture:** router 网关新增 `POST /v1/embeddings`（复用 resolveProvider+proxyRequest，e2e 透传，按 `usage.prompt_tokens` 计费）；agent/rag 改为应用级 key 指向 router；rag embedding 指向 router。设计：`dashboard/docs/plans/2026-09-06-router-llm-collect-design.md`。

**Tech Stack:** router gateway (Fastify/undici)；agent (Python 配置)；rag (.env)；部署 plink 到 192.168.31.34。

---

## 前置

- 仓库：`E:\workspace_ai\router`（gateway）、`E:\workspace_ai\agent`、`E:\workspace_ai\rag`（均可能非 git-clone 部署，重点是服务器文件）。
- router 现有 `POST /v1/chat/completions|/v1/responses|/v1/messages`；`providers/proxy.ts` `proxyRequest` 通用；`routes/helpers.ts` `resolveProvider`(admin /internal/models/resolve)+`reportUsage`。
- 服务器 gateway 端口 3100（`http://192.168.31.34:3100`），admin 3001。
- 应用级 key 需先手动 admin 建：`agent-app`/`rag-app`；并给 embedding 上游配 provider+model=inputPrice。

---

### Task 1: router 新增 `/v1/embeddings` 路由

**Files:**
- Create: `router/gateway/src/routes/embeddings.ts`
- Modify: `router/gateway/src/app.ts`

**内容：**
- `POST /v1/embeddings`，preHandler `[authenticate, rateLimit]`（照 chat.ts 结构）。
- 校验 `model` + `input`（string|string[]）。
- `resolveProvider(req, model)`；`proxyRequest(config.baseUrl, config.path || '/v1/embeddings', config.authType, config.apiKey, body, model, false)`。
- 读响应：非 2xx → 透传 `sendUpstreamError`（照 helpers）；2xx → 透传 JSON；同时累加计费：
  - 若响应 `usage.prompt_tokens` 存在 → `extractUsage('chat', body)` 得 tokensIn（输出 0）
  - 否则估算 `tokensIn ≈ sum(len(s)/4 for s in inputs)`
  - `reportUsage({ apiKey, providerId, model, tokensIn, tokensOut:0, cachedTokens:0, cost: calculateCost(...) , latencyMs })`
- 需要从 resolveProvider 拿到 providerId（ResolvedProvider 已有）+ model pricing（怎么拿：resolveProvider.config 是否带 pricing 或需另查——读 helpers.resolveProvider 返回结构，若无 pricing 则加 `/internal/models/resolve` 已有 `pricing` 字段，检查 ResolvedProvider.pricing；用它）。**以实际 ResolvedProvider 结构为准**（它可能已含 pricing）。
- 透传响应原文（不重算）。

**测试：** 现有 `router/gateway/test/*.test.ts`（看测试基建 Node test runner）；加 `embeddings.test.ts`：mock resolveProvider/proxyRequest/reportUsage：带 usage→按 prompt_tokens 计费、无 usage→按 len/4 估算、单/数组 input、非2xx 透传、401。若 gateway 测试 mock 困难则至少保证 tsc 通过 + 手动说明；尽量写可跑单测（参照 chat.ts 现有测试）。
**验证：** 在 `router/gateway` `npm run build`（tsc）+ 现有测试。
**Commit** (router): `feat(gateway): v1/embeddings 代理与计费`

---

### Task 2: 给 embedding 上游配置 provider/model（admin 数据）

**Files:**
- 数据类（无代码或 `router/admin` 种子/脚本）；说明为主，实现在服务器 admin 控制台或 SQL。
- **注意**：需要知道 embedding 上游真实地址。先探服务器 rag .env 的 `EMBEDDING_API_URL`，用它作为 router 的 embedding 上游（baseUrl/path 适配）。若该地址本身就是 OpenAI 兼容 `/v1/embeddings`，router 上游 baseUrl 指向其 host、path `/v1/embeddings`。
- 上游不足则需新增一个可用 embedding 服务（如 company gateway 的 embedding 端点）。

**内容（文档+命令）**：在 admin 建 provider（type OPENAI, baseUrl=<embedding host>, path=/v1/embeddings）+ model（name 如 `embedding-bge`，inputPrice 设单价，status ACTIVE）；用 admin API 或 SQL。给出可通过 admin 控制台(3102)或 `curl` 完成的步骤（读 admin providers/models 路由参数）。不写自动化代码，产出可执行命令/说明。

**验证：** `curl http://192.168.31.34:3001/...`（admin 接口）能看到新 provider/model；或控制台可见。
**Commit** (router, docs): 说明文档/或种子脚本（若 repo 有 seed 目录则加）。

---

### Task 3: 部署 router + 建应用级 key

**Files:** 服务器操作（plink）
**内容：**
1. 服务器 `~/ai-gateway`：同步 gateway 改动（pscp gateway/src/routes/embeddings.ts、app.ts）→ `docker compose build gateway` → `up -d --force-recreate gateway`（3100 短暂秒级窗口，可低峰）。
2. admin：建 `agent-app`、`rag-app` 两个 key（用 admin API `/api/keys` 或控制台，`X-Internal-Secret`/JWT），各配 daily/monthly quota；记录 raw key。
3. 验证：`curl http://127.0.0.1:3100/v1/embeddings -H 'Authorization: Bearer <rag-app>' -d '{"model":"embedding-bge","input":["hi"]}'` 返回 200 + 向量 或合理错误（embedding 上游未配则 4xx提示）。

**Commit:** 无（运维性）；如改服务器 compose/env 则备份。

---

### Task 4: agent 切 router chat

**Files:** 服务器 `~/agent/config/config.json`（llm.endpoints[].base_url→router、api_key→agent-app key）
**内容：**
- 读服务器 agent config.json 现状 → 改 `llm.endpoints[0].base_url = http://192.168.31.34:3100/v1`、`api_key = <agent-app>` （保留 model）。
- 备份原文件；改后 `docker restart agent`。
- 用 agent 某个已有接口（如 `GET /healthz` 或一次对话）验证走向 router（agent 侧日志或 router /usage 里出现 agent-app 的请求）。

**Commit:** 无（配置文件）。

---

### Task 5: rag 切 router chat + embedding

**Files:** 服务器 `~/rag/backend/.env`（或 deployment env）
**内容：**
- 探服务器实际 env 键名（`LLM_BASE_URL/LLM_API_KEY/LLM_MODEL`、embedding 键 `EMBEDDING_API_URL`(+KEY?)）——先前 config.py 见 `embedding_api_url`，确认是否有 key 字段。
- 改 `LLM_BASE_URL=http://192.168.31.34:3100/v1`、`LLM_API_KEY=<rag-app>`、`EMBEDDING_API_URL=http://192.168.31.34:3100/v1/embeddings`（若需 key 一并 `EMBEDDING_API_KEY=<rag-app>`）。
- 备份；重启 rag backend（docker / uvicorn）。
- 验证：rag 一次搜索/索引走 router（router usage 出现 rag-app）。

**Commit:** 无。

---

### Task 6: 冒烟与回归

**内容：**
- router：agent-app/rag-app key 调 chat + embedding 均通；rate/quota 生效。
- agent：对话正常（透过 router）；agent 自己的 usage 记录仍在。
- rag：检索正常（embedding 经 router）、对话正常。
- dashboard 本地模式：不变（每员工 key），仍可对话。
- 回归：其余（知识库页/市场页）不受影响。

**交付：** 冒烟结果记录；发现小修单独提交。

---

## 风险与备注

- embedding 上游未在 router 配置前，`/v1/embeddings` 对该 model 会 4xx——先配上游再验证（Task 2/3 顺序）。
- agent/rag 若依赖 embedding 服务返回特定 shape，收口后以 router 透传的响应为准——确认 rag 的 EmbeddingService 走 OpenAI `data[].embedding` 分支（服务器 rag.py 已兼容）。
- 计费精度：embedding 无 usage 时的 len/4 估算是粗口径；生产建议用有 usage 的上游或后续细化。
- 服务器为非 git 拷贝（ai-gateway/agent/rag 均 copy），同步用 pscp 直传 + 备份，git 仅本仓库（router）。
