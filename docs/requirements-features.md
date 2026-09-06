# AI 员工助手（agent）需求 → 功能清单

> 目的：把“公司级 AI 助手”的目标收敛成 需求域 → 功能清单(F-xx)，逐项标注
> 现状(✅已实现 / ⭕部分实现或需开启 / 🕐规划/依赖其它子系统)、所在模块、优先级，
> 作为演进与验收的统一依据。
> 上游参考：`../../docs/零号员工方案设计.md`、`dashboard/docs/design.md`、`docs/architecture-review-2026.md`。

## 一、产品目标（一句话）

面向全公司员工的在线智能体：**多人可同时对话、按人/权限隔离、全程审计、
管理员可观测（会话·token·性能）、可靠易运维**，能力由 agent 执行引擎 +
工具/技能/子代理 + rag/router/market/dashboard 平台协同提供。

## 二、干系角色

| 角色 | 关注点 |
|---|---|
| 员工用户 | 流畅对话、任务可执行、过程可见、能追问/确认、看到自己用量 |
| 部门管理员 | 本部门员工与工具/子代理权限、数据隔离 |
| 系统管理员 | 运行状态、并发会话、token/成本、性能、审计、异常恢复 |
| 运维 | 部署、升级、备份、监控告警、横向扩展 |
| 平台/渠道 | dashboard 桌面端、钉钉/飞书、网关 SSO、router 计量、rag/market |

## 三、需求清单（按域编号）

### R-A 对话与任务
- R-A1 多轮对话可续聊（不丢上下文）
- R-A2 回复逐 token 流式返回
- R-A3 过程可见：思考/工具/子代理实际执行过程实时展示
- R-A4 运行中可追问/危险操作确认（双向）
- R-A5 可执行多种工作：问答、代码、文件/数据处理、设备运维、工单/售后、知识检索、定时任务

### R-B 多用户与并发
- R-B1 全公司员工共用，多人在线同时对话
- R-B2 会话/记忆/文件按用户隔离，互不串扰
- R-B3 单用户/全员资源上限与公平调度
- R-B4 长会话上下文与 token 预算受控

### R-C 安全与权限
- R-C1 统一登录/SSO（企业身份）
- R-C2 基于角色的工具/子代理权限（RBAC）
- R-C3 危险操作需用户/管理员确认
- R-C4 密钥与敏感配置不落库、不越权

### R-D 审计与合规
- R-D1 会话内容全部落盘（可审计）
- R-D2 管理端按人/会话/时间检索、导出
- R-D3 用量与成本记录可回溯

### R-E 管理与可观测
- R-E1 管理员实时看：当前在跑会话数/在线用户/运行状态
- R-E2 token/成本/性能(P50/P95)大盘
- R-E3 日志流与异常定位

### R-F 性能与成本
- R-F1 延迟可接受、上下文压缩/缓存降本
- R-F2 配额/预算与用量归集（含 router 计量）

### R-G 可靠与运维
- R-G1 进程/worker 重启后会话可恢复续聊
- R-G2 数据备份/恢复、幂等迁移、灰度回滚
- R-G3 支持横向扩展/多实例

### R-H 渠道与前端
- R-H1 Web UI（对话/会话/用量/监控/管理）
- R-H2 桌面端 dashboard（平台统一入口）
- R-H3 钉钉/飞书等消息渠道

### R-I 非功能
- R-I1 可维护性（分层/模块化/文档同步）
- R-I2 自动化测试与回归保障
- R-I3 安全基线（默认口令/secret/防爆破）

## 四、功能清单（F-xx → 需求映射）

### 4.1 对话与过程
| 编号 | 功能 | 需求 | 现状 | 模块 | 优先级 |
|---|---|---|---|---|---|
| F1 | 多轮对话(会话/对话维度)与续聊 | R-A1/R-B4 | ✅ 对话(conversation_id)+DB恢复 | agent/session、web | P0 |
| F2 | SSE 流式 token 返回 | R-A2 | ✅ | web/server · ChatView | P0 |
| F3 | 思考过程展示 | R-A3 | ✅ reasoning 事件+折叠 | web/server · ChatView | P1 |
| F4 | 工具调用过程可见(名称/参数/结果) | R-A3 | ✅ tool_start/result+卡片 | web/server · ChatView | P0 |
| F5 | 子代理执行过程可见(流式+内部工具) | R-A3 | ✅ 顶层子代理卡片(深层受限) | subagent 转发 · ChatView | P0 |
| F6 | 运行中追问/回答(ask 双向) | R-A4 | ✅ SSE ask + /api/chat/answer(后端 e2e 通过,前端问答栏) | tools/ask_user、web | P1 |
| F7 | 危险操作审批(权限确认) | R-A4/R-C3 | 🕐 | 权限层+前端 | P1 |
| F8 | 多任务执行(工具/技能/子代理/团队) | R-A5 | ✅ | tools/skills/subagent/team | P0 |
| F9 | 定时任务/自主执行 | R-A5 | ✅ | plugins/scheduler、autonomous | P1 |
| F10 | 记忆/个性化 | R-A5 | ✅ 按 owner 隔离 | memory | P1 |

### 4.2 用户/并发/隔离
| 编号 | 功能 | 需求 | 现状 | 模块 | 优先级 |
|---|---|---|---|---|---|
| F11 | 登录(JWT, local) | R-C1/R-I3 | ✅ (默认口令需改) | web/auth+rbac | P0 |
| F12 | 企业 SSO/网关身份 | R-C1 | 🕐 平台侧(dashboard 网关/sso) | 平台 | P0 |
| F13 | RBAC(用户/角色/工具/子代理) | R-C2 | ✅ | security/rbac、executor | P0 |
| F14 | 会话/记忆/消息按用户隔离 | R-B2/R-C2 | ✅ | storage+api | P0 |
| F15 | 文件/工作区隔离 | R-B2 | ⭕ 需开 worker 池 | worker_pool、workspace | P0 |
| F16 | 多用户 Worker 池(实例隔离) | R-B1/R-B2/R-G3 | ⭕ 已实现、默认关; 公司部署需置 env/config 开启 | worker_pool | P0 |
| F17 | 资源上限/公平调度(worker 容量守护) | R-B3 | ✅ 溢出上限+忙碌等待+503(不再无界扩容) | worker_pool | P1 |
| F18 | 对话级限流/配额 | R-B3/R-F2 | 🕐 目前仅登录限流 | web | P1 |

### 4.3 安全与审计
| 编号 | 功能 | 需求 | 现状 | 模块 | 优先级 |
|---|---|---|---|---|---|
| F19 | 会话内容全量落盘(含归属列) | R-D1 | ✅ user/channel/conversation | storage | P0 |
| F20 | 管理端审计查询/导出 | R-D2 | ✅ /api/admin/sessions(+messages/export) | web+Monitor | P0 |
| F21 | 内部线程下钻 | R-D2 | ✅ threads API+UI | web+Monitor | P1 |
| F22 | 用量与成本记录 | R-D3/R-F2 | ✅ usage_records(含 duration/cache) | llm/usage、storage | P0 |
| F23 | 沙箱/命令与路径校验/密钥不落库 | R-C3/R-C4 | ✅ | security/sandbox | P0 |

### 4.4 管理可观测
| 编号 | 功能 | 需求 | 现状 | 模块 | 优先级 |
|---|---|---|---|---|---|
| F24 | 实时会话/在线用户/运行状态 | R-E1 | ✅ /api/admin/stats+总览(轮询) | web+Monitor | P0 |
| F25 | token/成本/性能大盘 | R-E2 | ✅ usage P50/P95 聚合 | web+Monitor | P0 |
| F26 | 个人用量视图 | R-E2 | ✅ /api/usage+UsageView | web | P1 |
| F27 | 日志流 | R-E3 | ✅ /api/logs/stream | web+Logs | P1 |
| F28 | 指标/告警(Prometheus 等) | R-E1/R-G3 | 🕐 | 平台监控 | P2 |

### 4.5 可靠与运维
| 编号 | 功能 | 需求 | 现状 | 模块 | 优先级 |
|---|---|---|---|---|---|
| F29 | 会话恢复续聊 | R-G1 | ✅ DB restore | core | P0 |
| F30 | 长任务续跑/回放 | R-G1 | 🕐 webhook 内存态→需 DB | web/webhook | P1 |
| F31 | DB 幂等迁移 | R-G2 | ✅ 启动自愈 | storage | P0 |
| F32 | 备份/恢复策略 | R-G2 | ⭕ 手动 .bak, 无制度 | 运维 | P1 |
| F33 | 健康检查/灰度回滚/多实例 | R-G2/R-G3 | ⭕ 单容器+健康 curl; 横向规划 | 部署 | P1/P2 |

### 4.6 前端与渠道
| 编号 | 功能 | 需求 | 现状 | 模块 | 优先级 |
|---|---|---|---|---|---|
| F34 | Web UI(对话/会话/用量/监控/审计) | R-H1 | ✅ | frontend | P0 |
| F35 | 切页不断流(keep-alive) | R-H1/R-A2 | ✅ | Layout+ChatView | P0 |
| F36 | 桌面端 dashboard 统一入口 | R-H2 | 🕐 平台侧(dashboard 项目) | 平台 | P0/P1 |
| F37 | 钉钉/飞书消息渠道 | R-H3 | ⭕ 代码在, 需开通与身份映射 | plugins | P1 |
| F38 | 知识问答(rag)/能力市场 | R-A5 | 🕐 平台侧对接 | 平台 | P1/P2 |

### 4.7 工程化
| 编号 | 功能 | 需求 | 现状 | 模块 | 优先级 |
|---|---|---|---|---|---|
| F39 | 分层模块化(拆 server/core 巨型文件) | R-I1 | 🕐 目标架构(评审 §5) · Wave B 已启动: admin/usage 拆入 routers | web/core | P1 |
| F40 | 测试/回归保障 | R-I2 | 🕐 现有部分测试损坏待修 | tests | P1 |
| F41 | 配置开关/env 化与文档同步 | R-I1/R-I3 | ✅ AGENT_WEB_POOL_SIZE 等+文档约定 | 各模块 | P0 |
| F42 | 安全基线(改默认口令/secret 注入/爆破防护) | R-I3/R-C4 | ⭕ 登录限流已设; 默认口令需改 | web/auth | P0 |

## 五、现状小结与下一步（对照演进路线）

- **已达成**：对话优先模型、用户/记忆/消息隔离、RBAC、审计落盘与查询导出、
  admin 大盘(会话/token/P50)、用量记录、SSE 流式与工具/子代理过程可视化、
  worker 池(默认关)、DB 恢复、幂等迁移。
- **待办优先级**（在评审 M1/M2 框架下）：
  1) worker 池公司默认开启 + 容量守护(F15/F16/F17)
  2) SSE 双向 ask/审批(F6/F7)
  3) webhook/长任务 DB 化与续跑(F30)
  4) 拆 server/core、run 语义收敛(F39)
  5) 修测试/补 e2e(F40)、安全基线(F42)、备份策略(F32)

> 每项落地后按“改码即改档”同步本文档与 `AGENTS.md`/`architecture-review-2026.md`。
