# 插件市场对接 dashboard（本地模式下载+混合执行）

日期：2026-09-05
状态：已评审通过

## 目标

员工在 dashboard「插件市场」页浏览市场能力并安装进**本地模式**内核：
- **skill**：下载解包 SKILL.md 到本地 skills 目录，本地 skills loader 直接用
- **mcp**：下载后解析 connection.json，追加本地 mcp.json，内核以现有 MCP 机制拉起
- **tool**：不下载；注册远程适配工具 `market:<name>` → market runtime invoke（带 SSO token）
- **agent**：下载 agent.json+PROMPT.md 到本地 agents 目录，作为「人设预设」在新建本地会话选用
- 身份复用 SSO OIDC（market 后端接双轨鉴权，与 router/rag 同构）；权限沿用 market 订阅(UserCapability)/access_policy。

## 能力包格式（market 后端 packages.py 约束）

| type | 必需文件 |
|---|---|
| skill | skill.json + SKILL.md |
| mcp | mcp.json + connection.json + tools.json + security.json |
| tool | tool.json + schema.json + implementation/tool.py |
| agent | agent.json + PROMPT.md |

## API 路径（market 全部在 /api 下，均已确认）

- 目录：`GET /api/capabilities`（portal）
- 详情：`GET /api/capabilities/{cap_id}`；版本 `.../versions`
- 下载：`GET /api/capabilities/{name}/download`（按 name，可能带 version 参数）
- 我的(已订阅/拥有)：`GET /api/my/capabilities`；添加 `POST /api/my/capabilities`；移除 `DELETE /api/my/capabilities`
- 运行 tool：`POST /api/runtime/tools/{name}/invoke`（RuntimeInvokeRequest）
- 鉴权：`Authorization: Bearer <SSO OIDC access_token>`（market 双轨后有效）

## 架构

```
MarketView.vue (浏览/订阅/安装管理)
  │  request('market', ...)   ← 主进程注入 OIDC token
主进程
  ├─ kernel/market.ts      createMarketClient(fetchImpl) — 可注入：download/subscribe/my/list
  ├─ kernel/installer.ts   install(cap): 按 type 分发:
  │     skill → 解包 SKILL.md → <data>/localagent/skills/<name>/SKILL.md
  │     mcp   → 解析 connection.json → 追加 <data>/localagent/mcp.json servers
  │     agent → 解包 agent.json+PROMPT.md → <data>/localagent/agents/<name>/
  │     tool  → 注册远程适配工具 market:<name>(调用注入的 invoke)
  ├─ ipc: localagent:market:install|uninstall|list|sync, market:download
  └─ skills loader / mcp.json / registry 现状无缝接入
```

- 安装目录：`<GATEWAY_DATA_DIR>/localagent/...`（与 sessions/skills/mcp.json 同根），数据子目录 `market/`。
- 版本：`<name>@<version>` 目录/条目；重装=覆盖。
- 卸载：删目录/移 mcp 条目/摘 remote tool。
- agent 人设：`localagent/agents/<name>/PROMPT.md` → 新建本地会话时可选「市场人设 <name>」注入 systemPrompt（store/create session 增加可选 persona 源）。

## D-ready / 安全

- market OIDC 轨验 aud/iss/exp；capability 下载前仍走其订阅与可见性（open 可见）。
- 解包路径安全：拒绝 zip slip（解包路径必须在目标目录内，参照 dashboard pathsafe 思想）。

## 本期不做

agent 远程 A2A 消费、零号员工接入、workflow、tool 本地 python、评分/评论 UI 完整交互、market 前端独立登录路径迁移。

## 部署

market 在 192.168.31.34:8093 重建（docker），`.env`/compose 加 `SSO_ISSUER=http://192.168.31.45:8091`、`SSO_AUDIENCE=dashboard-gateway`、`SSO_JWKS_URI=http://192.168.31.45:8091/.well-known/jwks.json`；dashboard marketUrl 指向 `http://192.168.31.34:8093`。
