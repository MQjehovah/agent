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

| 路由 | 功能 |
| ---- | ---- |
| /login | 登录(JWT) |
| /chat | 对话(SSE 流式,工具调用过程可见) |
| /sessions | 会话历史(查看/删除) |
| /kanban | 任务看板(四列,移动/删除) |
| /scheduler | 定时任务(CRUD/启停) |
| /memories | 记忆(搜索/删除/提案审批) |
| /logs | 运行日志(SSE 实时流) |
| /admin | 用户管理(RBAC,admin 角色) |
| /settings | 改密码 / 状态检查 |
