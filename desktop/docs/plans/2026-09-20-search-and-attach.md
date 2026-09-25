# dashboard 搜索与本地附件 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让侧栏 Ctrl+K 真正能搜（本地过滤），并让本地模式下能把文件附进消息。

**Architecture:** 全新渲染层组件 `CommandPalette.vue`（el-dialog 全窗覆盖）+ 布局接线；附件走新增的主进程选文件 IPC，复制进会话工作区后把相对路径写进消息文本。

**Tech Stack:** Electron + electron-vite + Vue 3 + TypeScript + Element Plus 2.14 + Pinia；测试用 node:test + tsx。

**设计依据：** `dashboard/docs/plans/2026-09-20-search-and-attach-design.md`

---

## 前置事实（已核实，直接用）

- 搜索按钮：`src/layouts/WorkbenchLayout.vue:142-146`（`.side-item disabled`, `title="即将上线"`）。
- 键盘处理：`WorkbenchLayout.vue:67-72`（仅 Ctrl+N），注册/注销在 `:76-84`。
- 会话数据：`src/stores/sessions.ts` 的 `sessions` getter（`:37-58`），字段 `id/mode/title/createdAt/updatedAt/messageCount/workspace`；布局已有 `openSession`（`:37-50`）与每 15s 的 `refresh()`（`:78-80`）。
- 知识库：`request<WikiIndex>('rag', '/api/wiki')`（`KnowledgeView.vue:40`），类型在 `src/api/types.ts:138-174`。
- 市场：`request<MarketCapPage>('market', '/api/capabilities?page_size=100&page=N')`（`MarketView.vue:105-112`）。
- 选目录 IPC 先例：`handleWorkspacePick`（`electron/main/kernel/ipc.ts:441-447`），通道 `localagent:workspace:pick`（`preload/index.ts:19`），白名单在 `preload/index.ts:4-31`，强制在 `:56-61`。
- 附件 chip：`ChatView.vue:193-197`；`isLocal` 在 `:101`；发送在 `:19-24`；textarea 在 `:185-192`。
- 本地会话工作区：`src/stores/chat.ts:114-131`（`localWorkspace`），kernel 路径安全在 `electron/main/kernel/pathsafe.ts:23-37`。

样式：2 空格缩进、无分号、单引号、中文注释与 UI 文案、英文标识符。

---

## Task 1: 命令面板组件

**Files:** Create `src/components/CommandPalette.vue`

**Step 1:** 新建组件，`props: { modelValue: boolean }` + `emit('update:modelValue')`（`v-model` 控制显隐），`emit('navigate', path: string)`、`emit('open-session', item: SessionListItem)`。

结构要求：

```vue
<template>
  <el-dialog :model-value="modelValue" @update:model-value="emit('update:modelValue', $event)"
             :show-close="false" width="560px" top="12vh" class="cmd-palette" append-to-body>
    <el-input ref="inputRef" v-model="query" placeholder="搜索会话、知识库、能力，或输入命令…"
              clearable @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)"
              @keydown.enter.prevent="run()" @keydown.esc="close()" />
    <div class="cmd-list" v-if="flat.length">
      <template v-for="group in groups" :key="group.name">
        <div class="cmd-group" v-if="group.items.length">
          <div class="cmd-group-title">{{ group.name }}</div>
          <button v-for="item in group.items" :key="item.key"
                  class="cmd-item" :class="{ active: item.key === activeKey }"
                  @mouseenter="activeKey = item.key" @click="run(item)">
            <el-icon v-if="item.icon"><component :is="item.icon" /></el-icon>
            <span class="cmd-item-title">{{ item.title }}</span>
            <span class="cmd-item-sub" v-if="item.subtitle">{{ item.subtitle }}</span>
          </button>
        </div>
      </template>
    </div>
    <el-empty v-else description="没有匹配结果" :image-size="60" />
  </el-dialog>
</template>
```

数据与过滤（`<script setup lang="ts">`）：
- `commands`：静态数组，条目形如 `{ key, group:'命令', title, icon, run: () => emit('navigate', path) }`，覆盖 `/chat`、`/sessions`、`/knowledge`、`/market`、`/usage`、`/settings`，以及「新建任务」。
- `sessions`：来自 `useSessionsStore().sessions`，映射为可搜索条目（`title` 匹配 id/标题），`run` 触发 `emit('open-session', item)`；`subtitle` 显示模式（本地/远程）与时间。
- `wiki`：`watch(() => modelValue)` 首次为 true 时若未加载则 `request<WikiIndex>('rag', '/api/wiki')`，把 `categories[].pages[]` 摊平成条目（`{ id, title, summary }`），`run` 触发 `emit('navigate', '/knowledge')`（本期不深链到具体页；若实现简单可带 query）。加载失败静默降级（该组为空，不弹错误）。
- `market`：同理首次拉 `/api/capabilities?page_size=100`，摊平 `items`，`run` 触发 `emit('navigate', '/market')`。
- `groups` computed：对每个分组按 `query` 做**本地** `toLowerCase().includes()` 过滤；空 `query` 时命令组全显示、其余组只显示前 8 条；有 `query` 时每组最多 8 条。
- `flat` computed：把 `groups` 里有条目的项按顺序摊平，用于 ↑/↓ 导航；`activeKey` 初始为第一项；`move(delta)` 循环移动；`run(item?)` 执行 `item ?? 当前 activeKey 对应项` 后关闭面板。
- `close()`：`emit('update:modelValue', false)`。
- 打开时聚焦输入框（`watch(modelValue)` → `nextTick(() => inputRef.value?.focus())`）并清空 `query`。

**Step 2:** 从 `@element-plus/icons-vue` 引入用到的图标（`ChatDotRound`/`Clock`/`Collection`/`MagicStick`/`TrendCharts`/`Setting`/`SwitchButton`/`Search`），与 `WorkbenchLayout.vue:5-17` 的既有用法一致。

**Step 3:** 校验：`npm run typecheck` 通过。

**Step 4:** 提交：`feat(dashboard): 新增命令面板组件`

---

## Task 2: 布局接线（Ctrl+K + 按钮 + 键盘守卫）

**Files:** Modify `src/layouts/WorkbenchLayout.vue`

**Step 1:** 引入 `CommandPalette`，新增 `const paletteVisible = ref(false)`（与既有 `profileVisible` 并列）。

**Step 2:** 把搜索按钮（`:142-146`）改为可点：

```vue
<button class="side-item" title="搜索" @click="paletteVisible = true">
  <el-icon :size="15"><Search /></el-icon>
  <span class="side-label">搜索</span>
  <span class="side-kbd">Ctrl+K</span>
</button>
```

**Step 3:** `onKeydown` 增加 Ctrl+K 分支，并补输入焦点守卫：

```ts
function isTypingTarget(el: EventTarget | null): boolean {
  const node = el as HTMLElement | null
  if (!node) return false
  const tag = node.tagName?.toLowerCase()
  return tag === 'input' || tag === 'textarea' || node.isContentEditable === true
}

function onKeydown(e: KeyboardEvent) {
  if (isTypingTarget(e.target)) return
  if (e.ctrlKey && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    paletteVisible.value = true
    return
  }
  if (e.ctrlKey && e.key.toLowerCase() === 'n') {
    e.preventDefault()
    newTask()
  }
}
```

**Step 4:** 在模板末尾挂载面板并接线：

```vue
<CommandPalette v-model="paletteVisible"
                @navigate="onPaletteNavigate"
                @open-session="onPaletteOpenSession" />
```

```ts
function onPaletteNavigate(path: string) { paletteVisible.value = false; router.push(path) }
function onPaletteOpenSession(item: SessionListItem) { paletteVisible = false; openSession(item) }
```

（`openSession` 是布局内既有函数 `:37-50`，直接复用；`SessionListItem` 从 `src/stores/sessions.ts` 导入。）

**Step 5:** 校验 `npm run typecheck`，并手动确认：点按钮与 Ctrl+K 都能打开；焦点在 textarea 内按 Ctrl+K 不触发。

**Step 6:** 提交：`feat(dashboard): Ctrl+K 命令面板接线并补输入焦点守卫`

---

## Task 3: 选文件 IPC + 复制进工作区

**Files:** Modify `electron/main/kernel/ipc.ts`, `electron/preload/index.ts`; Create `electron/main/kernel/attachments.ts`; Test `test/kernel/attachments.test.ts`

**Step 1（TDD）:** 先写 `test/kernel/attachments.test.ts`，针对一个**纯函数**模块 `electron/main/kernel/attachments.ts` 覆盖：
- `isAllowedAttachment(name)`：白名单扩展名通过（`.txt .md .json .csv .py .ts .js .go .java .sh .yaml .yml .log .pdf .png .jpg .jpeg .gif .webp .docx .xlsx .pptx`），`.exe`/`.dll`/无扩展名/空名被拒。
- `attachmentTargetName(original, ts)`：生成 `<ts>-<safeName>`，其中 `safeName` 去掉路径分隔符与 `..`，保留扩展名。
- `formatAttachmentNotice(relPaths)`：生成追加到消息的文本，形如 `\n\n已附带文件：\n- .attachments/173...-a.txt`（多文件各一行）。
先跑测试确认失败（模块不存在）。

**Step 2:** 实现 `attachments.ts`（纯函数，无 Electron 依赖，便于测试）：`ALLOWED_EXTS`、`MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024`、上述三个函数。

**Step 3:** 在 `electron/main/kernel/ipc.ts` 增加 handler（仿 `handleWorkspacePick`）：

```ts
async function handleFilePick(event: IpcMainInvokeEvent): Promise<{ canceled: boolean; paths: string[] }> {
  const win = BrowserWindow.fromWebContents(event.sender)
  const options = { properties: ['openFile', 'multiSelections'] as Array<'openFile' | 'multiSelections'> }
  const result = win ? await dialog.showOpenDialog(win, options) : await dialog.showOpenDialog(options)
  if (result.canceled || result.filePaths.length === 0) return { canceled: true, paths: [] }
  return { canceled: false, paths: result.filePaths }
}
```
注册为 `ipcMain.handle('localagent:file:pick', handleFilePick)`（与既有通道注册处并列）。

再加一个把文件复制进工作区的 handler `localagent:attach:import`，入参 `{ sessionId: string; paths: string[] }`，返回 `{ imported: { relPath: string; name: string }[]; skipped: { path: string; reason: string }[] }`：
- 由 `sessionId` 解析出该本地会话的工作区（读 `electron/main/kernel/session.ts` 的会话 meta，取 `workspace`；**不要**信任渲染层传来的 workspace）；
- 目标目录 `<workspace>/.attachments/`，`mkdir -p`；
- 逐个：`isAllowedAttachment` → `stat` 校验大小 ≤ `MAX_ATTACHMENT_BYTES` → `copyFile` 到 `attachmentTargetName(...)`；任一步失败则记入 `skipped` 并继续；
- 返回 `relPath` 形如 `.attachments/<ts>-<name>`（相对工作区）。

**Step 4:** `electron/preload/index.ts` 的 `ALLOWED_CHANNELS` 加入 `'localagent:file:pick'` 与 `'localagent:attach:import'`。

**Step 5:** 校验：`npm test`（新测试通过、既有 179 不回归）、`npm run typecheck`、`npm run build`。

**Step 6:** 提交：`feat(dashboard): 本地模式选文件并复制进会话工作区`

---

## Task 4: 输入区接线 + 附件提示

**Files:** Modify `src/views/ChatView.vue`

**Step 1:** `+ 文件` chip 改为可点（仅本地模式）：

```vue
<span v-if="isLocal" class="composer-chip" :class="{ busy: attaching }" @click="pickAttachments">
  {{ attaching ? '导入中…' : '+ 文件' }}
</span>
<span v-else class="composer-chip disabled" title="远程模式暂不支持附件">+ 文件</span>
```

**Step 2:** 实现 `pickAttachments()`：
1. `const picked = await window.desktop.invoke<{canceled:boolean; paths:string[]}>('localagent:file:pick')`；取消直接返回。
2. 需要当前本地会话 id（`chat.sessionId`）与工作区（`chat.localWorkspace`）；缺失时 `ElMessage.warning('请先选择工作区创建本地会话')` 并返回。
3. `const res = await window.desktop.invoke<{imported:{relPath:string;name:string}[]; skipped:{path:string;reason:string}[]}>('localagent:attach:import', { sessionId, paths: picked.paths })`。
4. `imported` 非空则把相对路径暂存到组件的 `pendingAttachments`（用于在输入区上方显示小 chip 列表、可单个移除）。
5. `skipped` 非空则 `ElMessage.warning(\`已跳过 ${n}\` 个文件：\` + 原因汇总)`。
6. 全程 `attaching` loading 状态 + try/catch → `ElMessage.error`。

**Step 3:** 发送时把附件并入文本：在 `send()`（`:19-24`）里，若 `pendingAttachments` 非空，用 `formatAttachmentNotice`（从主进程模块导出的纯函数在同一渲染层无法直接引；**改为在 `ChatView.vue` 内内联同样的格式化逻辑**，或在 `src/utils/` 放一份共享实现，两边共用同一段代码 —— 选择后者：`src/utils/attachments.ts` 导出 `formatAttachmentNotice`，主进程与渲染层都从各自可访问的路径复制同一实现并加注释说明；本期允许两处同构实现，但必须由单测锁定格式）。发送后清空 `pendingAttachments`。

**Step 4:** 校验 `npm run typecheck` + `npm run build`；手动验收：本地模式选一个 `.txt` 发送，确认消息里出现附件路径，且（在 local 会话里）让模型 `file.read` 该路径能读到内容。

**Step 5:** 提交：`feat(dashboard): 输入区接入本地附件`

---

## Task 5: 验收

**Step 1:** 全量：`npm test`、`npm run typecheck`、`npm run build`；记录 counts。

**Step 2:** 逐条核对设计文档《验收标准》1–6，结果写入 `docs/plans/2026-09-20-search-and-attach-design.md` 末尾的「实施结果」小节。

**Step 3:** 补充文档：`docs/design.md` 路线图里把「搜索」「附件」从规划改为已实现；`README.md:24` 同步。（注意 design.md 里还残留已删除的 `server/` 描述 —— 本次一并修正为实际的 `upstream.ts` 结构，因为它是同一批文档债。）

**Step 4:** 提交：`docs(dashboard): 搜索与附件实施结果并修正过时结构说明`

---

## 已知限制（本期不做）

- 远程 agent 模式无附件（需后端上传端点 + 传输层改造）。
- 附件复制到工作区后不自动清理。
- agent 会话无标题，面板只能按 id 命中。
- 知识库/市场在面板首次打开时各拉一次列表（非按键级请求）。
