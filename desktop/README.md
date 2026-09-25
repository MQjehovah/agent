# Dashboard —— 员工 AI 工作台(桌面端)

零号员工平台的**统一接入层与员工门户**:面向公司全体员工的类 ZCode 桌面端应用,把
[agent](../agent)(执行引擎)、[rag](../rag)(知识库)、[router](../router)(算力网关)、
[market](../market)(能力市场)四大子系统的能力,以桌面端产品形态交付给每一位员工。

- 设计方案:[docs/design.md](docs/design.md)
- 总体规划:[../docs/零号员工方案设计.md](../docs/零号员工方案设计.md)

## 技术栈

Electron + electron-vite · Vue 3 + TypeScript + Element Plus + Pinia · electron-builder · 主进程 IPC 上游代理(凭证不出主进程)

## 快速开始

```bash
npm install        # 首次安装(建议已配置 ELECTRON_MIRROR 或 npmmirror)
npm run dev        # 开发模式(主进程 + 渲染端 HMR)
npm run build      # 构建产物(三进程)
npm run dist       # 打 Windows 安装包(electron-builder)
```

启动后进入「设置」页,填入各子系统服务地址;企业模式下完成「统一登录」即可开始流式对话。
知识库(rag)、插件市场(market)、用量(router)页面均已上线。Ctrl+K 搜索为本地过滤(仅本地数据,
无按键级请求);文件附件仅本地模式支持。

## 身份与上游(主进程)

桌面端没有独立后端/BFF:渲染层经 preload 的 IPC 把请求交给主进程 `upstream.ts`,由主进程注入
凭证后直连四大子系统。登录为 OIDC SSO(系统浏览器授权码 + loopback 回调),凭证只留在主进程
并以 `credstore.ts` 加密落盘;agent 账号按工号 JIT 开通,router apikey 由 id_token 向 router admin 换取。

```
渲染层 ──invoke──▶ 主进程 ──注入凭证──▶ agent / rag / market / router
                       ├ identity.ts   # OIDC SSO + 凭证持有
                       ├ agent-jit.ts  # agent 账号 JIT
                       ├ upstream.ts   # IPC 上游代理 + SSE 透传
                       └ usage.ts      # router 用量聚合
```

相关配置(设置页 / 环境变量):各子系统地址、router admin 地址与 Internal Secret、OIDC issuer/client。

## 目录结构

```
dashboard/
├── electron/main/       # 主进程:窗口、上游代理(upstream.ts)、身份(identity.ts)、用量(usage.ts)、kernel/
├── electron/preload/    # contextBridge 暴露最小 API(通道白名单)
├── src/                 # 渲染进程(Vue 3):api 客户端 / stores / layouts / components / utils / views
├── docs/design.md       # 设计方案
└── docs/统一身份与SSO方案.md  # 统一身份平台方案(自研 SSO / 钉钉→LDAP / 中台接入)

../sso/                  # 自研统一认证服务(OIDC Provider,独立部署)
```

企业模式登录:设置页「企业账号 SSO 登录」→ 系统浏览器完成 OIDC 认证 → loopback 回调自动返回桌面端。

## 路线图(三步走)

| 步骤 | 内容                                                                                     |
| ---- | ---------------------------------------------------------------------------------------- |
| 一   | **统一登录**:主进程 OIDC SSO + agent JIT + router apikey(已实现;待公司 IdP 选型)(当前)     |
| 二   | **对接线上零号员工/Router**:全员可对话,算力经 router 统一计量,员工→配额 Key 映射          |
| 三   | **能力市场接入**:能力浏览/订阅/调用、知识问答(rag)、用量视图(router);知识库/市场/用量已上线(口径:仅 router 可归属用量) |

**已实现(跨步骤)**:Ctrl+K 搜索(本地过滤,仅本地数据、无按键级请求)、本地模式文件附件(仅本地模式)。
