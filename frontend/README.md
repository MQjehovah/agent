# Agent Web UI(Vue 3)

零号员工的 Web 界面(Vue 3 + Vite + Element Plus),由 FastAPI(`src/web/server.py`)托管。

## 开发

```bash
cd frontend
npm install
npm run dev        # http://localhost:5180,代理 /api 与 /webhook 到 127.0.0.1:8080
```

## 构建

```bash
npm run build      # 产物输出到 ../src/web/static_vue,FastAPI 自动优先托管
```

构建后无需重启 agent(StaticFiles 直接读文件);旧版单页仍可通过 `/legacy` 访问作为回退。

## 视图

左侧菜单分两组：

- **个人空间（所有登录用户）**：工作台 / 对话 / 定时任务 / 记忆管理 —— 组内数据一律**个人口径**，admin 亦只看自己（我的会话/定时任务/记忆/用量均按 `web:{uid}` 隔离）
- **运维与管理（仅 admin，整组隐藏 + 路由守卫拦截）**：运行监控 / 会话管理 / 任务看板 / 日志 / Webhook / 用户管理 / 设置 —— 组内为**全站/系统视角**

| 路由 | 分组 | 功能 |
| ---- | ---- | ---- |
| /login | — | 登录(JWT) |
| /dashboard | 个人空间 | 工作台（个人工作台：我的会话含运行中、我的记忆、我的定时任务；**用量已并入本页**；Agent 状态/系统健康见运维「运行监控」） |
| /chat | 个人空间 | 对话(SSE 流式 token;工具调用动态状态;子代理执行过程实时显示;切换页面不断流,keep-alive 保活;左侧历史会话仅本人) |
| /scheduler | 个人空间 | 定时任务(CRUD/启停,仅本人) |
| /memories | 个人空间 | 记忆管理(搜索/删除;仅展示本人私有 + 全局公共,不再列出他人私有) |
| /sessions | 运维与管理 | 会话管理(admin:全部用户活跃+落盘历史合并,运行中/查看/删除) |
| /kanban | 运维与管理 | 任务看板(四列,移动/删除) |
| /monitor | 运维与管理 | 运行监控(全站运行中会话/实时会话/worker池/MCP/token大盘/性能/会话审计导出) |
| /logs | 运维与管理 | 运行日志(SSE 实时流) |
| /webhook | 运维与管理 | Webhook 任务(外部触发任务状态) |
| /admin | 运维与管理 | 用户管理(RBAC) |
| /settings | 运维与管理 | 改密码 / 状态检查 |

> 鉴权：`运维与管理` 整组为 admin 专属(导航按登录角色 `agent_role` 隐藏;路由守卫对 `meta.admin` 路由非 admin 重定向 `/dashboard`)。
> 个人口径约定：`/api/sessions`、`/api/agent/sessions/history`、`/api/memories` 默认 `scope=mine`（admin 亦只看自己）；运维页显式传 `scope=all`/`view=all` 才切全站。总览个人数据走 `/api/my/overview`。
