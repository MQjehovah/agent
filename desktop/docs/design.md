# Dashboard 设计方案(员工 AI 工作台桌面端)

| 项目     | 内容                                                         |
| -------- | ------------------------------------------------------------ |
| 文档版本 | v0.2                                                         |
| 编制日期 | 2026-09-04                                                   |
| 文档类型 | 新子系统设计方案                                              |
| 代码位置 | `E:\workspace_ai\dashboard`                                  |
| 上游文档 | [零号员工方案设计](../../docs/零号员工方案设计.md)             |
| 变更记录 | v0.2 接入网关(BFF)与三步走路线图;UI 对齐 ZCode 工作台形态<br>v0.3 统一身份定稿:自研 SSO(sso/)+ 钉钉→OpenLDAP 同步(deploy/)+ 网关 OIDC/JIT,详见 [统一身份与SSO方案](统一身份与SSO方案.md) |

---

## 一、定位

Dashboard 是零号员工平台的**第五个子系统:统一接入层与员工门户**,补齐总体架构中"接入层"缺失的一环。

```
┌──────────────────────────── 接入层 ────────────────────────────┐
│  CLI │ Web UI │ 钉钉 │ 飞书 │ Webhook │ 定时任务                │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  dashboard 桌面端(本项目)                                 │  │
│  │  面向全公司员工的统一 AI 入口 —— 类 ZCode 桌面工作台        │  │
│  └──────────────────────────────────────────────────────────┘  │
└───────────────────────────────┬────────────────────────────────┘
                                ▼
┌──────────────────────────── 执行层 ────────────────────────────┐
│  agent 执行引擎(协调者)                                        │
└───────────────┬───────────────────────────────┬────────────────┘
                ▼                               ▼
┌───────────────────────────┐   ┌───────────────────────────────┐
│ rag 企业知识库(记忆)       │   │ router 算力网关(治理)          │
└───────────────────────────┘   └───────────────────────────────┘
                │                               │
                └───────────────┬───────────────┘
                                ▼
┌───────────────────────────────────────────────────────────────┐
│ market 能力市场(生态)                                          │
└───────────────────────────────────────────────────────────────┘
```

一句话职责:**把 agent / rag / router / market 四大子系统的能力,以桌面端产品形态交付到每一位员工手里。**

与现有"接入层"各渠道的区别:

| 渠道         | 受众         | 局限                        | dashboard 补齐                      |
| ------------ | ------------ | --------------------------- | ----------------------------------- |
| agent Web UI | 开发者/试点  | 原生 HTML/JS 单页,体验粗糙  | 产品级 UI、多面板工作台             |
| CLI          | 开发者       | 门槛高                       | 零门槛安装、图形界面                |
| 钉钉/飞书    | 全员消息场景 | 受消息形态限制,能力受限      | 完整会话、文件、工具调用可视化       |

**不做的事**(边界原则,对齐 5.2 节):

- 不实现任何 Agent 执行逻辑——只做 agent 执行引擎的客户端;
- 不直接调用 LLM 供应商——模型调用一律经 agent / router;
- 不做知识存储——知识一律走 rag;
- 不做能力审核——能力上架流程留在 market,dashboard 只是消费端。

## 二、对标 ZCode 的产品形态

参考 ZCode 桌面端的核心交互,映射到本平台:

| ZCode 概念           | dashboard 对应                                | 依赖的后端                    |
| -------------------- | --------------------------------------------- | ----------------------------- |
| 会话(Session)        | 会话列表 + 持续对话                           | agent `/api/sessions`         |
| 流式对话             | SSE 流式回复、Markdown 渲染、工具调用过程可见  | agent `/api/chat/stream`      |
| 技能/插件/MCP        | 能力市场浏览、订阅、一键启用                   | market `/api/capabilities` 等 |
| 知识库上下文         | 知识问答入口、引用溯源展示                     | rag 检索 API                  |
| 配额/用量            | 个人用量与费用视图(已实现)                     | router 用量 API               |
| 设置                 | 服务端地址、令牌、外观配置                     | 本地存储                      |

## 三、技术选型

| 层           | 选型                                        | 理由                                                           |
| ------------ | ------------------------------------------- | -------------------------------------------------------------- |
| 桌面壳       | **Electron**                                | 公司栈无 Rust,Tauri 会引入全新工具链;Electron 生态成熟、自动更新方案完备 |
| 构建工具     | electron-vite(主进程/preload/渲染端一体构建) | 事实标准,HMR、三进程统一配置                                    |
| 前端         | Vue 3 + TypeScript + Vite + Element Plus    | 对齐公司"前端统一"选型,复用 market/rag 前端经验                 |
| 状态管理     | Pinia                                       | Vue 3 官方推荐                                                  |
| Markdown渲染 | markdown-it(`html: false` 防注入)            | LLM 输出渲染刚需                                                |
| 打包分发     | electron-builder                            | Windows 安装包 + 自动更新(后续接内网更新源)                     |

**不引入独立后端服务**(无 BFF、无本地 HTTP 代理)。渲染进程不直接跨域请求四大子系统,而是经 preload 暴露的 IPC 把请求交给主进程,由主进程按服务注入凭证后直连上游:

```
渲染进程(浏览器沙箱)
   │  window.desktop.invoke('upstream:request' | 'upstream:stream:start')  ← preload 唯一 IPC 通道
   ▼
Electron 主进程 upstream.ts
   │  解析上游地址 + 注入凭证(authFor)
   ├─→ agent  http://<host>:8080   Authorization: Bearer <agent JWT>
   ├─→ rag    http://<host>:8092   Authorization: Bearer <OIDC access_token>
   ├─→ market http://<host>:8093   Authorization: Bearer <OIDC access_token>
   └─→ router http://<host>:3100   Authorization: Bearer <router apikey>
```

收益:绕开 CORS 与混合内容限制;SSE 流式透传(以 `streamId` 经 `upstream:event` 通道推原始文本块);凭证集中在主进程注入;渲染进程零网络权限(ContextIsolation + sandbox + 无 nodeIntegration)。

### 3.1 身份与凭证(主进程,企业模式)

全员推广时,四大子系统不能把账号直接交给员工终端,登录与凭证统一收敛在主进程 `identity.ts`:

```
系统浏览器 ──授权码──▶ 公司统一身份(OIDC Provider)
    ▲                        │ code(loopback 回调,固定 127.0.0.1:8090)
    │                        ▼
Electron 主进程 ── identity.ts ── ① OIDC 授权码 + JWKS 验签 → id/access/refresh token
                                  │ ② 身份映射:sub(工号) JIT 开通 agent 账号 → agent JWT(agent-jit.ts)
                                  │ ③ router apikey:用 id_token 向 router admin /internal/sso/exchange 换取
                                  └ rag/market 复用 OIDC access_token
```

关键设计:

- **凭证不出主进程**:OIDC token / agent JWT / router apikey 只存在于主进程内存,并以 `credstore.ts` 加密落盘(`identity.json`,应用重启免登录);渲染层仅通过 `auth:me` 拿到用户信息;
- **agent JWT 不下发**:`agent-jit.ts` 按工号 JIT 开通/换取 agent 账号,仅用于对 agent 上游注入;
- **按服务注入**:`upstream.ts` 的 `authFor()` 决定 agent→agent JWT、router→apikey、rag/market→OIDC access_token;
- **OIDC 参数可分发**:`oidc-config.ts` 解析 issuer/client/secret(环境变量优先,其次配置文件,无内置兜底);
- **登录策略可扩展**:以 OIDC 为唯一入口,钉钉等渠道由上游 OIDC Provider 承担,桌面端零改动;
- 已知限制:agent 的 kanban / scheduler / todos 等仍是全局共享,按人隔离需 agent 侧配合(P1);market / rag 自有账号体系的映射逐步接入。

> 历史说明:早期版本曾计划引入独立接入网关(`server/`,Node 原生 http)。该目录已删除,其职责(登录、凭证持有、服务代理)现由主进程 `identity.ts` + `upstream.ts` 承担。

## 四、模块设计

```
dashboard/
├── docs/design.md           # 本文档
├── electron/
│   ├── main/                # 主进程
│   │   ├── index.ts         # 窗口生命周期、安全基线、IPC 注册
│   │   ├── upstream.ts      # 上游代理(IPC 直达,主进程注入凭证,含 SSE 透传)
│   │   ├── identity.ts      # OIDC SSO 登录与凭证持有(主进程内存 + 加密落盘)
│   │   ├── agent-jit.ts     # agent 账号 JIT 开号与 agent JWT
│   │   ├── oidc-config.ts   # OIDC issuer/client 解析(环境变量优先)
│   │   ├── credstore.ts     # 凭证加密存储
│   │   ├── usage.ts         # router 管理端用量聚合
│   │   ├── store.ts         # userData 下 JSON 配置持久化
│   │   └── kernel/          # 本地模式 kernel:工具 / IPC / 会话 / 权限
│   └── preload/index.ts     # contextBridge 暴露最小 API 面(通道白名单)
├── src/                     # 渲染进程(Vue 3)
│   ├── api/                 # 四大子系统 API 客户端(唯一出网口)
│   │   ├── client.ts        # IPC 传输封装(request / streamRequest / probe)
│   │   ├── agent.ts         # chat / sessions / auth
│   │   └── types.ts
│   ├── components/CommandPalette.vue   # Ctrl+K 命令面板
│   ├── stores/              # Pinia:settings / sessions / chat
│   ├── layouts/WorkbenchLayout.vue   # 侧栏 + 内容区(类 IDE 布局)
│   ├── views/               # ChatView / SessionsView / KnowledgeView / MarketView / UsageView / SettingsView / LoginGate
│   ├── utils/               # markdown / attachments
│   └── styles/
├── electron.vite.config.ts
├── electron-builder.yml
└── package.json
```

### 4.1 主进程

- **窗口**:1360×860 默认(最小 960×640),`contextIsolation: true`、`nodeIntegration: false`、`sandbox: true`、`webSecurity: true`,隐藏标题栏 + 主题自适应 overlay,加载渲染端;
- **上游代理**:`upstream.ts` 按 service 拼接上游地址并注入凭证;普通请求走 `upstream:request`,SSE 请求走 `upstream:stream:start`(`streamId` + `upstream:event` 推块,`upstream:stream:abort` 中断);上游地址从本地配置读取,支持运行时切换;
- **身份与凭证**:`identity.ts`(OIDC SSO + 凭证持有)、`agent-jit.ts`(agent 账号 JIT)、`oidc-config.ts`、`credstore.ts`(加密落盘),详见 3.1;
- **本地模式 kernel**:`kernel/` 承载工具调用、权限、会话与选目录/选文件 IPC;
- **配置持久化**:`%APPDATA%/dashboard/config.json`(各子系统地址、router admin、Internal Secret、OIDC 参数、主题等),读写收敛在 `store.ts`。

### 4.2 渲染进程

- **WorkbenchLayout**:左侧窄图标栏(导航)+ 可折叠会话列表 + 主内容区;深色主题优先;全局 Ctrl+K 搜索(焦点在输入控件内不拦截)、Ctrl+N 新建任务;
- **CommandPalette**(`components/CommandPalette.vue`):命令面板,本地过滤命令/会话/知识库/市场;会话回车经布局 `openSession` 打开,其余 `router.push`;知识库/市场首次打开各拉一次列表并缓存;
- **ChatView**:
  - 输入框(Enter 发送 / Shift+Enter 换行)、停止按钮(中断 SSE);
  - 消息流:Markdown 渲染、代码高亮、工具调用/思考过程折叠展示(agent SSE 事件已含事件流);
  - 新会话/续聊:`session_id` 贯穿;
  - **本地模式附件**:选文件后复制进会话工作区 `.attachments/`,以相对路径文本随消息发送(kernel `file_read` 可读);远程模式入口置灰;
- **SessionsView**:调 `GET /agent/api/sessions` 列表,查看历史消息、删除会话;
- **KnowledgeView / MarketView / UsageView**:知识库(rag)浏览、能力市场浏览/订阅、个人用量视图;
- **SettingsView**:四个服务地址 + router admin/Internal Secret + OIDC 参数,连通性测试按钮;
- **LoginGate**:企业 SSO 登录门。

### 4.3 与四大子系统的接口清单(P0)

| 功能       | 方法 + 路径                              | 说明                    |
| ---------- | ---------------------------------------- | ----------------------- |
| 企业登录   | OIDC 授权码;`POST /agent/api/auth/login`(JIT) | 主进程 `identity.ts`/`agent-jit.ts`,渲染层只拿用户信息 |
| 流式对话   | `POST /agent/api/chat/stream`            | SSE,核心链路           |
| 会话列表   | `GET /agent/api/sessions`                |                         |
| 会话消息   | `GET /agent/api/sessions/{id}/messages`  |                         |
| 删除会话   | `DELETE /agent/api/sessions/{id}`        |                         |
| 任务列表   | `GET /agent/api/tasks`                   | 任务面板用              |
| 能力浏览   | `GET /market/api/capabilities`           | 能力市场页              |
| 知识检索   | rag 检索 API                             | 知识问答页              |
| 用量查询   | router 用量 API                          | 已实现(仅 router 可归属用量,零 router 改动) |

## 五、路线图(2026-09 按三步走重排)

**总原则:登录是企业入口的前置条件;先能安全地对话,再谈能力生态。**

| 步骤 | 主题 | 内容 | 状态 / 退出标准 |
| ---- | ---- | ---- | ---------------- |
| 一 | **统一登录** | 主进程 OIDC SSO(`identity.ts`)+ agent 账号 JIT(`agent-jit.ts`)+ router apikey 交换。原独立接入网关(`server/`)已删除,职责收敛到主进程 | ✅ 已实现;⏳ 待定:公司 IdP 选型 |
| 二 | **对接线上零号员工/Router,全员可对话** | 桌面端指向生产 agent;agent 的 LLM 端点切到 router 网关统一计量;员工→router 配额 Key 映射;企业配置分发(安装包内置地址/OIDC 参数) | ⏳ 下一步;依赖:生产 agent 地址、router 发 Key 流程 |
| 三 | **能力市场接入** | 能力市场页(浏览/订阅/启用)、知识问答页(rag)、用量视图(router) | ✅ 知识库/市场/用量视图已实现;⏳ 能力调用与调试待做 |

> 用量视图口径:仅 router 可归属用量,零 router 改动。

**已实现(跨步骤)**:Ctrl+K **搜索**命令面板——本地过滤命令/会话/知识库/市场,**仅本地数据、无按键级请求**(首开各拉一次列表);**文件附件**——本地模式可选文件并复制进会话工作区 `.attachments/`,以相对路径随消息发送(**仅本地模式**,远程模式入口置灰)。实现详见 [搜索与本地附件设计](plans/2026-09-20-search-and-attach-design.md)。

后续(原 P2/P3 保留):SSO 全面化后的会话审计、自动更新(内网源)、崩溃上报;快捷指令、托盘常驻、全局悬浮球。

### 5.1 测试基线(步骤一退出标准)

可离线验证的纯逻辑(如 OIDC 参数解析 `test/kernel/oidc-config.test.ts`、用量聚合 `test/kernel/usage.test.ts`)由 `npm test` 覆盖并全绿;`npm run typecheck`、`npm run build` 通过。SSO 登录依赖真实 IdP,需联调验证,当前无自动化烟测。

## 六、风险与应对

| 风险                                  | 应对                                                          |
| ------------------------------------- | ------------------------------------------------------------- |
| agent Web API 面向单人设计,多员工并发 | 登录/凭证已收敛主进程(3.1);会话按人隔离需 agent 侧配合,短期以部门为单位灰度 |
| Electron 安装包体积/内网分发           | electron-builder + 内网更新源;nsis 压缩                       |
| 各子系统地址/令牌分发成本              | P1 支持"企业配置链接"一键导入(scanned/https schema)            |
| 渲染 LLM 输出的 XSS                   | markdown-it `html:false`;CSP 限制 `default-src 'self'`         |
| SSE 经代理的断流                      | 主进程管道层心跳 + 渲染端指数退避重连                          |
