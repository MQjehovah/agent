# LLM / Embedding 收口 router（应用级出口治理）

日期：2026-09-06
状态：已评审通过

## 目标

把全公司 LLM 出口统一收口到 `router/`（LLM 网关）：
- **chat**：agent、rag 改走 router（应用级 key）——已在侧的 dashboard 本地模式（每员工 key）不动
- **embedding**：router 新增 `POST /v1/embeddings`，rag 的 embedding 收口；按输入 token 计费
- **rerank**：第二期（router 需按 RerankerService 私有格式代理，本期保留现状）
- **key 维度**：router 按"平台/应用"计量（agent 一把、rag 一把、本地模式按员工）；用户维度留在各平台自己用户体系，不穿透到 router
- **agent 不限流**：不额外加路由层限流，仅平台级配额

## 计费口径确认

- embedding 响应带 `usage.prompt_tokens`（OpenAI 兼容）；复用现有 `extractUsage('chat', body)`（读 `prompt_tokens`）+ `calculateCost`，输出 token 计 0。
- 嵌入上游的 Provider/Model 配 `inputPrice`（每百万 token 单价）；**无需扩展 usage.ts**。
- 若某 embedding 上游不返回 `usage`，则按输入 token 估算（在 embedding 路由内用 `arrayToTokens` 类估算兜底或对 input 长度估算——实施时定）。

## 架构

```
                    ┌──────────── router (LLM 网关) ────────────┐
agent ──chat────────▶│  /v1/chat/completions (已有)             │
rag   ──chat────────▶│  /v1/chat/completions                     │
rag   ──embedding───▶│  /v1/embeddings (新增)                    │
dashboard 本地模式 ──▶│  /v1/chat/completions (已有, 每员工 key) │
                    └──────────────────────────────────────────┘
                          │ resolveProvider(apiKey, model)
                          │ proxyRequest(baseUrl,path,authType,apiKey,body)
                          ▼ 上游 provider 表里配置
                  智谱 coding / ai.rosiwit / deepseek / embedding 服务
```

## 3.1 router 改动

`gateway/src/routes/embeddings.ts`（新）：
- `POST /v1/embeddings`，preHandler `[authenticate, rateLimit]`
- body `{model, input, ...}`；校验 model + input（string 或数组）
- `resolveProvider(req, model)` → `proxyRequest(config.baseUrl, config.path 或 '/v1/embeddings', config.authType, config.apiKey, body, model, false)`
- 透传响应给客户端；响应体里若有 `usage.prompt_tokens` → 在 reportUsage 时 extractUsage('chat', body) 计 cost（`pricing` 用 model 的 inputPrice，output=0）
- 无 usage 时估算：`tokensIn ≈ input 字符串长度/4 * 条数`（简单估算，或按 provider 返回）
- 挂 `reportUsage`（现有上报）记录到 `apiKeyId/model`

`gateway/src/app.ts`：注册 `embeddingsRoutes`。

## 3.2 上游配置（admin Provider/Model）

- 新增 provider：embedding 服务（如已有的 embedding 上游地址 / OpenAI 兼容），`type='OPENAI'`，`baseUrl` 指向其 `/v1` 根或全端点，`path='/embeddings'`
- 新增 model：如 `text-embedding-3-small` 类，`inputPrice` 设好（每百万 token），`providerId` 关联
- 现有 chat 上游（智谱 coding / ai.rosiwit / deepseek）已是 provider/model，无需改

## 3.3 agent 收口

- `agent/config/config.json` `llm.endpoints[].base_url` → `http://192.168.31.34:3100/v1`（router），`api_key` → agent 应用级 key
- 保留 agent 自身 usage_records/user 维度；不加额外限流
- 部署：改服务器 `~/agent/config/config.json` + 重启 agent 容器

## 3.4 rag 收口

- `rag/backend/.env`（服务器）：`LLM_BASE_URL`→router、`LLM_API_KEY`=rag key、`EMBEDDING_API_URL`→router `/v1/embeddings`、`EMBEDDING_API_KEY`=rag key（若配置项存在，读 rag config 确认键名）
- rerank 第二期
- 部署：改服务器 rag .env + 重建/重启 rag backend

## 3.5 key 分配

- 手动在 admin 控制台（或 admin API）建两把**应用级** key：`agent-app`、`rag-app`，各自配额（daily/monthly）按公司预算设；写给各自配置
- dashboard 本地模式：不动

## 测试

- router：`/v1/embeddings` 单元/集成（假 provider 返回 usage → 计费；无 usage → 估算；单 input 与数组 input；401/配额）
- 现有 router 测试不回归
- 冒烟：agent/rag 用新 key 调通 chat；rag embedding 走 router 返回向量

## 本期不做

- rerank 收口（第二期，需按 RerankerService 私有格式）
- agent/rag 的用户维度穿透 router
- router 对 embedding 的响应缓存/专用限流策略

## 部署步骤（服务器 192.168.31.34）

1. router 重建（gateway 新端点）→ 重启 gateway；admin 建 `agent-app`/`rag-app` key + embedding 上游/模型
2. agent：改 config.json base_url/api_key → 重启 agent 容器
3. rag：改 .env（LLM/embedding）→ 重建/重启 rag backend
4. 验证三家 chat + rag embedding
