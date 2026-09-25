# 插件市场对接 dashboard 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** dashboard「插件市场」页浏览/订阅市场能力并安装到本地模式内核（skill/mcp 本地、tool 远程适配、agent 人设）；market 后端接 SSO OIDC 双轨鉴权。

**Architecture:** market `get_current_user` 加 OIDC 轨（JWKS 验 SSO token→按 User.username=工号 自动建号）；dashboard 主进程 `kernel/market.ts`+`installer.ts` 下载/解包/注册；渲染层 `MarketView.vue` 页 + IPC。设计：`dashboard/docs/plans/2026-09-05-market-local-design.md`。

**Tech Stack:** market Python/FastAPI + PyJWT；dashboard Electron TS；Vue3/Element Plus。

---

## 前置说明

- 两个 git 仓库：`E:\workspace_ai\market`（Python，backend/app）、`E:\workspace_ai\dashboard`（TS）。market Python 解释器同 rag 用法 `D:\Python\Python312\python.exe`（如 market venv 独立则以其为准——先看 market 有无 venv/运行说明，`python -m pytest` 若可跑则用它，否则只做可 import/语法验证 + 手动说明）。
- 已确认 API 路径与能力包格式见设计文档；实现者需再读一遍对应源文件核实字段名与请求体。
- market 双轨参考 rag 已落地实现（`rag/backend/app/core/sso_auth.py` + `jwt_utils.get_current_user` 双轨）——**照搬套路，按 market 的 async SQLAlchemy 与 User 模型适配**。
- 风格：Python 120 列/ruff；TS 无分号/单引号/2空格/中文注释。

---

### Task 1: market Settings 增加 SSO 配置 + sso_auth 模块

**Files:**
- Modify: `market/backend/app/config.py`
- Create: `market/backend/app/core/sso_auth.py`（若无 core 包则放 `app/sso_auth.py` 与 auth.py 同级）
- Test: `market/backend/tests/test_sso_auth.py`（若 market 无 tests 目录则新建；参照 rag 的 RSA 自签 + file:// JWKS helper）

**内容：**
1. config 加 `sso_issuer/sso_audience/sso_jwks_uri: str = ""`（market config 形态先读 config.py，可能是 pydantic settings 或轻量类）。
2. `verify_sso_token(token) -> dict`：JWKS 拉取(缓存 300s)/file://+http 支持/kid 匹配与无 kid 兜底/iss+aud+exp+sub 校验/`SsoAuthError`。尽量复用 rag 版逻辑（语言相同直接移植改 import 路径）。
3. 测试：合法/坏签名/错aud/过期/未配置，~6 用例。

**验证：** pytest（新文件）绿；ruff。
**Commit** (market): `feat: SSO/OIDC 配置与 JWKS 验签模块`

---

### Task 2: market get_current_user 双轨

**Files:**
- Modify: `market/backend/app/auth.py`
- Test: `market/backend/tests/test_auth_sso.py`

**内容：** `get_current_user`（async，PyJWT HS256，sub=user UUID）：先走原 decode（保留 401 语义），失败→ `verify_sso_token` → `username=sub(工号)` 查 User，无则自动建号（is_active=True，role 取现有最低角色如 'user'——读 models.User.role 枚举确认），返回 User；`get_current_user_optional` 同步支持（浏览匿名可见）。并发建号 IntegrityError 回查兜底。**下游 `require_runtime_access/can_use` 零改动。**

**测试：** SSO 建号/复用/坏 token 401/HS256 老轨防回归（用临时 sqlite async 库，参照 market 现有测试写法或最小 async test）。
**验证：** 新测试绿；ruff；`python -c "import app.main"` 可 import。
**Commit** (market): `feat: get_current_user OIDC 双轨(HS256|SSO)`

---

### Task 3: dashboard 主进程 market client + installer

**Files:**
- Create: `dashboard/electron/main/kernel/market.ts`（`createMarketClient(deps)` 可注入 fetchImpl：`listMy()/subscribe(name)/unsubscribe(id)/download(name, version?): bytes`，Bearer 注入与错误折叠——参照 `rag.ts` 的 `createRagSearcher` 结构）
- Create: `dashboard/electron/main/kernel/installer.ts`（`installCapability(client, cap)` 分发 + zip 解包安全 + mcp.json 合并 + skills 落盘 + agent 目录）
- Test: `dashboard/test/kernel/market.test.ts`、`dashboard/test/kernel/installer.test.ts`

**内容：**
- market.ts：路径 `/api/my/capabilities`、`/api/capabilities/{name}/download`（version 参数先读 portal.py 确认）；返回结构容错；非2xx 取 detail。
- installer.ts：
  - `unzipSafe(bytes, destDir)`：拒绝 `..`/绝对路径条目（zip slip），条目拼在 dest 内。
  - skill：解包取 `SKILL.md` 写到 `<data>/localagent/skills/<name>/SKILL.md`（front-matter 若无则补 `---\nname/description\n---`，description 从 capability.description；若已有则以包内为准）。
  - mcp：读 `connection.json`（shape 先读 market 样例/校验器），追加 `<data>/localagent/mcp.json` servers（name=capability.name 去重覆盖），保留已存在 server。
  - agent：解包 `agent.json`+`PROMPT.md` 到 `<data>/localagent/agents/<name>/`。
  - tool：不落盘——由 ipc 层在 registry 注册远程适配（见 Task 4）。
  - 返回已安装清单（可 JSON）。
- `<data>` = `process.env.GATEWAY_DATA_DIR`（ipc 传入，installer 不读 env，用参数）。

**测试：** zip 解包（含 zip-slip 恶意样本拒绝）、skill front-matter 补写、mcp.json 合并去重、各目录落位（tmp data dir）；market client 假 fetch URL/header/错误折叠。
**验证：** `npm test`（107+新增）、`npm run typecheck:node`。
**Commit** (dashboard): `feat(kernel): 市场客户端与本地安装器`

---

### Task 4: IPC 装配 + 远程 tool 适配 + agent 人设

**Files:**
- Modify: `dashboard/electron/main/kernel/ipc.ts`
- Modify: `dashboard/electron/main/kernel/tools.ts` 或新 `tools-market.ts`
- Modify: `dashboard/electron/main/kernel/session.ts`（LocalSession 支持可选 persona）或 store.createSession 参数
- Modify: `dashboard/electron/main/store.ts`（AppConfig 增 marketUrl 默认 http://192.168.31.34:8093）

**内容：**
- IPC 通道：`localagent:market:install {name}` / `:uninstall {type,name}` / `:list` / `:subscribe`/`:unsubscribe`（或并入 install 自动订阅）/ `:personas`（列本地 agents 人设）。
- registry 远程适配：`tool` 类型的 market capability → 工具 `market:<name>`，execute 注入 `createMarketClient` 调 `/api/runtime/tools/{name}/invoke`（kind 默认 'read'，description=能力描述），注册在 `ensureRegistry`（联网拉取可失败降级不阻塞）。
- install 后刷新：skill/mcp 立即生效（skills loader 每次调用重扫；mcp 下次会话 chat 时 connect——告知用户重启会话生效即可）。
- agent 人设：新建本地会话 `sessions:create` 增可选 `persona?: {name, prompt}`；ipc 组装时若指定 persona 且本地 agents/<name>/PROMPT.md 存在则作 systemPrompt 前缀。
- uninstall：删对应目录 / 移 mcp.json 条目 / registry 摘 `market:` 工具（重建 registry 时机）。

**验证：** `npm test`、`npm run typecheck`、`npm run build` 全绿。
**Commit** (dashboard): `feat(kernel): market 安装/卸载 IPC 与远程 tool 适配/人设`

---

### Task 5: dashboard「插件市场」页 + 侧栏入口

**Files:**
- Create: `dashboard/src/views/MarketView.vue`
- Modify: `dashboard/src/router/index.ts`（/market）、`dashboard/src/layouts/WorkbenchLayout.vue`（navMain 加市场，icon MagicStick；从 navSoon 移除）、`dashboard/src/api/types.ts`（MarketCapability 等类型）、样式 main.css（`.market-*`，语义变量）

**内容：** 目录卡片列表（`request('market','/api/capabilities')`）→ type 徽标(agent/tool/skill/mcp)、评分、已装/已订阅态；详情（现有字段）；「订阅」切换；「安装」按钮：按 type 调 `localagent:market:install`（skill/mcp/agent 本地；tool 提示走远程适配）；「已安装」页签：list + 卸载；加载/错误/未登录态同知识库页风格；persona 安装后出现在新建本地会话的「人设」选择（ChatView 新会话控件或设置，最小实现为新建本地会话时 el-select 可选本地人设——若 Task 4 已支持，此处暴露）。

**验证：** typecheck + build + test。
**Commit** (dashboard): `feat(ui): 插件市场页(浏览/订阅/安装)`

---

### Task 6: 部署 + 冒烟

- market：服务器 192.168.31.34:8093 部署（git pull 或 pscp 改动文件 + docker 重建，compose/.env 加 SSO 三件套——沿用 rag/router 流程）；验证 401/双轨/IdP 可达。
- dashboard：marketUrl=`.34:8093`；冒烟：市场页浏览→订阅 open 能力→skill 安装→本地模式会话能触发该技能；mcp 安装→新会话工具出现；tool 订阅→`market:<name>` 工具调用通。
- 回归：其余功能不受影响。

**Commit**：按仓库分步小提交。

---

## 风险与备注

- market 测试基建可能为空（无 tests 目录/pytest 配置）——Task 1/2 若跑不了 pytest，至少保证模块可 import + ruff，并说明如何手动验证。
- mcp 本地拉起依赖其 connection.json 语义（command/args vs url）——安装器仅落配置，连接行为交给既有 MCP 客户端；若某 mcp 包需 docker 属环境问题，安装器不负责拉起 docker。
- market 部署版本需与本地开发一致：部署用 git 分支主；服务器若为 git clone 卡认证则退回 pscp 直传方案。
- tool 远程调用有 marketplace/订阅鉴权，失败折叠为 ok:false，不阻塞对话。
