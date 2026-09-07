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

左侧菜单（两级平铺，无折叠子菜单）：

- **个人空间（所有登录用户，平铺）**：工作台 / 对话 / 定时任务 / 记忆管理 —— 组内数据一律**个人口径**，admin 亦只看自己：会话=本人全部渠道(`mine`)，定时任务=仅我创建的(排除 static 系统级)，记忆=仅本人私有(**不含 global 公共**)，均按 `web:{uid}` 隔离
- **运维与管理（仅 admin，整组隐藏 + 路由守卫拦截，平铺三个菜单项）**：运行监控 / 用户管理 / 设置 —— 其中「运行监控」为**综合 Tab 页**(运维全站视角)，用户管理/设置与个人空间项同层平铺
- 其余运维页(会话管理/日志/Webhook/任务看板)不再出现在菜单，仅保留 admin 直达路由，主入口统一收拢到「运行监控」Tab 页

| 路由 | 分组 | 功能 |
| ---- | ---- | ---- |
| /login | — | 登录(JWT)；落地默认 `/dashboard`(个人空间) |
| /dashboard | 个人空间 | 工作台（个人工作台：我的会话含运行中、我的私有记忆、我的定时任务；**用量已并入本页**；Agent 状态/系统健康见运维「运行监控」） |
| /chat | 个人空间 | 对话(SSE 流式 token;工具/子代理动态状态;keep-alive 保活)。左侧历史会话=**本人全部渠道**(web/钉钉/其它,带渠道徽标)：web 会话可点开续聊；钉钉等外部渠道会话**只读**(可看历史，发送框禁用；后端 `/api/chat(stream)` 对非 web session 亦拒绝写回) |
| /scheduler | 个人空间 | 定时任务(CRUD/启停,仅我创建；`?scope=all`=全量含 static，admin 直达) |
| /memories | 个人空间 | 记忆管理(搜索/删除；仅展示我的私有，不含 global；`?view=all`=全量含 global+全部人私有，admin 直达) |
| /monitor | 运维与管理 | 运行监控 = **综合 Tab 页**(支持 `?tab=overview\|sessions\|scheduler\|memories\|webhook\|logs` 直达/刷新保持)：总览(全局系统视图：全站运行中/实时会话/worker 池/MCP/token 大盘/性能/会话审计导出) / 会话管理(运行中 N + 全部用户历史,查看/删除) / 定时任务(全量 scope=all) / 记忆管理(全量 view=all) / Webhook / 日志 |
| /admin | 运维与管理 | 用户管理(RBAC) |
| /settings | 运维与管理 | 改密码 / 状态检查 |
| /sessions | (admin 直达,不入菜单) | 会话管理(同 /monitor?tab=sessions，全部用户活跃+落盘历史合并,运行中/查看/删除) |
| /logs | (admin 直达,不入菜单) | 运行日志(SSE 实时流，同 /monitor?tab=logs) |
| /webhook | (admin 直达,不入菜单) | Webhook 任务(外部触发任务状态，同 /monitor?tab=webhook) |
| /kanban | (admin 直达,不入菜单) | 任务看板(需求未纳入「运行监控」Tab，保留直达) |

> 鉴权：`运维与管理` 整组为 admin 专属(导航按登录角色 `agent_role` 隐藏；路由守卫对 `meta.admin` 路由与 `/scheduler?scope=all`、`/memories?view=all` 的全量模式，非 admin 一律重定向 `/dashboard`)。
> 个人口径约定：`/api/sessions`、`/api/agent/sessions/history`、`/api/memories`、`/api/scheduler/tasks` 默认 `scope=mine`(admin 亦只看自己；记忆 mine 不含 global，定时 mine 不含 static)；运维页显式传 `scope=all`/`view=all` 才切全站。总览个人数据走 `/api/my/overview`。
> 左下角显示当前登录用户姓名 + 角色(经 `/api/auth/me`)。
