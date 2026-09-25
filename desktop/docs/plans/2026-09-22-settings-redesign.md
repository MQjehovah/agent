# 设置界面优化 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把 dashboard 设置页改造为「分区卡片 + 吸底操作条」，补齐企业账号认证区与操作反馈，视觉走克制专业风（方向 A）。

**Architecture:** 仅改渲染层单文件 `src/views/SettingsView.vue`（template + script + `<style scoped>`）。不动 `stores/settings.ts`、`electron/main/*`。复用既有能力：`settings.loginSso/logout/save`、`probe()`、`window.desktop.invoke('localagent:workspace:pick')`、`agentApi`（不需要）。

**Tech Stack:** Vue 3 `<script setup>` + TypeScript + Element Plus（`el-form/el-input/el-button/el-tooltip`）+ Electron IPC。

**设计依据：** `docs/plans/2026-09-22-settings-redesign-design.md`

---

### Task 1: 交互逻辑（script）

**Files:**
- Modify: `src/views/SettingsView.vue`（整段 `<script setup>`）

要点：
1. 表单快照对比 → `dirty` 计算属性（主题不计入）
2. `saving` / `testing` 状态；保存成功后刷新快照
3. URL 校验：`isHttpUrl()` + `urlErrors` 计算属性；非法时阻止保存并高亮
4. 探测状态扩展为四态：`idle | probing | ok | fail`，记录 message/latency
5. `pickWorkspace()`：`window.desktop.invoke('localagent:workspace:pick')` 回填路径
6. 登录/退出：`doSsoLogin()` 接上按钮；`doLogout()` 二次确认（`ElMessageBox.confirm`）后 `settings.logout()` 并清空对话（复用 `useChatStore().newSession()`）

**验证：** `npm run typecheck`

### Task 2: 模板（分区卡片 + 吸底操作条）

**Files:**
- Modify: `src/views/SettingsView.vue`（`<template>`）

结构：
```
header(标题 + 说明) → 账号与企业认证卡 → 服务地址卡 → 本地模式卡 → 外观卡 → 关于卡 → 吸底操作条
```
- 每张卡片：`<section class="card">` + 图标 + 标题 + 说明
- 账号卡：头像 + 姓名/工号/部门/角色 + 状态点 + 按钮组（登录/重新登录/退出）
- 服务行：`label` + `el-input` + 状态徽标（`el-tooltip` 显示耗时/原因）
- 主题：两个预览块（点击即时生效）
- 吸底条：左侧未保存提示，右侧「测试连通性」「保存」

**验证：** `npm run build`（编译 SFC）

### Task 3: 样式（视觉规范）

**Files:**
- Modify: `src/views/SettingsView.vue`（追加 `<style scoped>`）

- 卡片 14px 圆角 / 18–20px 内边距 / 标题行（图标 16px + 标题 15px/600 + 说明 12.5px）
- 表单 `label-position="top"`；字段说明 12px secondary
- 服务行 `grid: 1fr auto`；徽标固定宽右对齐
- 吸底条 `sticky bottom:0` + `backdrop-filter: blur(8px)` + 上边框
- 颜色全用 `--el-*` 变量；动效仅 `transition .15s`

**验证：** `npm run build` + 深/浅主题各看一遍（手动）

### Task 4: 验证与提交

```bash
npm run typecheck     # node + web 均通过
npm run build         # electron-vite 构建通过
npm test              # 203 用例通过
git add src/views/SettingsView.vue
git commit -m "feat(dashboard): 设置界面重构(分区卡片/吸底操作条/认证区补齐)"
```

手动清单（用户在窗口内验证，Ctrl+R 重载）：登录 → 状态点与工号显示；改动 → 未保存提示；保存 → 提示消失；测试连通性 → 5 个徽标（含失败态）；选择目录 → 回填；主题切换即时生效；退出登录 → 回对话并回登录门。
