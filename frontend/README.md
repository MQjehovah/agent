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

- **工作台（所有登录用户）**：总览 / 对话 / 定时任务 / 记忆
- **运维与管理（仅 admin，整组隐藏 + 路由守卫拦截）**：会话管理 / 看板 / 运行监控 / 日志 / Webhook / 用户管理 / 设置

| 路由 | 分组 | 功能 |
| ---- | ---- | ---- |
| /login | — | 登录(JWT) |
| /dashboard | 工作台 | 总览（个人工作台：当前用户、Agent 状态、我的会话含运行中、我的记忆、定时任务；**用量已并入本页**） |
| /chat | 工作台 | 对话(SSE 流式 token;工具调用动态状态;子代理执行过程实时显示;切换页面不断流,keep-alive 保活;左侧含历史会话) |
| /scheduler | 工作台 | 定时任务(CRUD/启停,仅本人) |
| /memories | 工作台 | 记忆(搜索/删除/提案审批) |
| /sessions | 运维与管理 | 会话管理(admin:全部用户活跃+落盘历史合并,运行中/查看/删除) |
| /kanban | 运维与管理 | 任务看板(四列,移动/删除) |
| /monitor | 运维与管理 | 运行监控(实时会话/token大盘/性能/会话审计导出) |
| /logs | 运维与管理 | 运行日志(SSE 实时流) |
| /webhook | 运维与管理 | Webhook 任务(外部触发任务状态) |
| /admin | 运维与管理 | 用户管理(RBAC) |
| /settings | 运维与管理 | 改密码 / 状态检查 |

> 鉴权：`运维与管理` 整组为 admin 专属(导航按登录角色 `agent_role` 隐藏;路由守卫对 `meta.admin` 路由非 admin 重定向 `/dashboard`)。
> 普通用户会话/用量接口自动按身份(`web:{uid}`)隔离;总览个人数据走 `/api/my/overview`(admin 亦只看自己)。
