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

左侧菜单：

- **个人空间（所有登录用户，一级直接列项）**：工作台 / 对话 / 定时任务 / 记忆管理 —— 组内数据一律**个人口径**，admin 亦只看自己：会话=本人全部渠道(`mine`)，定时任务=仅我创建的(排除 static 系统级)，记忆=仅本人私有(**不含 global 公共**)，均按 `web:{uid}` 隔离
- **运行监控（仅 admin，整组隐藏 + 路由守卫拦截，一级可展开二级）**：运行监控 / 会话管理 / 定时任务(全量) / 记忆管理(全量) / 日志 / Webhook / 用户管理 / 设置 —— 组内为**全站/系统视角**（定时=scope=all 含 static，记忆=view=all 含 global+全部人私有）

| 路由 | 分组 | 功能 |
| ---- | ---- | ---- |
| /login | — | 登录(JWT)；落地默认 `/dashboard`(个人空间) |
| /dashboard | 个人空间 | 工作台（个人工作台：我的会话含运行中、我的私有记忆、我的定时任务；**用量已并入本页**；Agent 状态/系统健康见运维「运行监控」） |
| /chat | 个人空间 | 对话(SSE 流式 token;工具/子代理动态状态;keep-alive 保活)。左侧历史会话=**本人全部渠道**(web/钉钉/其它,带渠道徽标)：web 会话可点开续聊；钉钉等外部渠道会话**只读**(可看历史，发送框禁用；后端 `/api/chat(stream)` 对非 web session 亦拒绝写回) |
| /scheduler | 个人空间 | 定时任务(CRUD/启停,仅我创建；`?scope=all` 由「运行监控」进入=全量含 static) |
| /memories | 个人空间 | 记忆管理(搜索/删除；仅展示我的私有，不含 global；`?view=all` 由「运行监控」进入=全量含 global+全部人私有) |
| /monitor | 运行监控 | 运行监控(全站运行中会话/实时会话/worker池/MCP/token大盘/性能/会话审计导出) |
| /sessions | 运行监控 | 会话管理(admin:全部用户活跃+落盘历史合并,运行中/查看/删除) |
| /logs | 运行监控 | 运行日志(SSE 实时流) |
| /webhook | 运行监控 | Webhook 任务(外部触发任务状态) |
| /admin | 运行监控 | 用户管理(RBAC) |
| /settings | 运行监控 | 改密码 / 状态检查 |

> 鉴权：`运行监控` 整组为 admin 专属(导航按登录角色 `agent_role` 隐藏；路由守卫对 `meta.admin` 路由与 `/scheduler?scope=all`、`/memories?view=all` 的二级全量模式，非 admin 一律重定向 `/dashboard`)。
> 个人口径约定：`/api/sessions`、`/api/agent/sessions/history`、`/api/memories`、`/api/scheduler/tasks` 默认 `scope=mine`(admin 亦只看自己；记忆 mine 不含 global，定时 mine 不含 static)；运维页显式传 `scope=all`/`view=all` 才切全站。总览个人数据走 `/api/my/overview`。
> 左下角显示当前登录用户姓名 + 角色(经 `/api/auth/me`)。
