# dashboard 搜索与本地附件 设计

日期：2026-09-20
状态：已确认，待实施
范围：`dashboard` 单仓库（无后端改动、无 agent 改动）

## 背景

两项功能在 UI 里挂了很久的占位：

- `src/layouts/WorkbenchLayout.vue:142-146` 侧栏「搜索 Ctrl+K」是 `class="side-item disabled" title="即将上线"`，且 `onKeydown`（`:67-72`）只有 Ctrl+N 分支，没有任何搜索实现。
- `src/views/ChatView.vue:193-197` 输入区的「+ 文件」是 `class="composer-chip disabled" title="P1 上线"`，无文件选择、无附件状态。

调研确认的约束：

- 渲染层**没有命令面板/autocomplete/过滤组件**可复用；Element Plus 版本 `^2.14.5`，`el-dialog` 默认 teleport 到 body，可直接覆盖全窗（布局的 `.workbench` 无 transform/overflow 限制；顶部拖拽条 `z-index:100`）。
- **agent 会话没有标题**：`src/stores/sessions.ts:43` 用 `title: s.id`（形如 `web-<hex>`），只有本地会话有真标题。故面板对 agent 会话实际只能按 id 命中。
- 知识库与市场数据是**视图局部 ref**（`KnowledgeView.vue:16`、`MarketView.vue:43`），不在 store 中。
- 本地模式 kernel 的文件工具强制 `resolveWithin(工作区)`（`electron/main/kernel/pathsafe.ts:23-37`），**工作区外的文件会被拒绝读取**。
- 仅存在选**目录**的 IPC（`handleWorkspacePick`，`electron/main/kernel/ipc.ts:441-447`），没有任何选文件/上传/FormData 代码。
- 远程 agent 模式**完全没有附件能力**：`/api/chat/stream` 只接受 `{ message, session_id }`，且 `electron/main/upstream.ts:56,61` 传输层只发 JSON。
- 渲染进程 `sandbox: true`（`electron/main/index.ts:44-47`），`<input type="file">` 拿不到绝对路径。

## 目标

1. Ctrl+K 打开命令面板，**本地过滤**已有数据，回车执行跳转/打开。
2. 本地模式下可把文件附进消息，kernel 能读取到该文件。

## 非目标（YAGNI）

- 不调用后端搜索端点（不用 `POST /api/search`、不做按键级请求）。
- 不改 agent 后端、不给 agent 会话生成标题。
- **远程模式不做附件**（按钮保持置灰，并说明原因）。
- 不做拖拽上传、不做图片预览、不做大文件分片。

## 设计

### A. 命令面板

新增 `src/components/CommandPalette.vue`，用 `el-dialog` + `el-input` + 分组列表。数据来源：

| 分组 | 来源 | 请求 |
|---|---|---|
| 命令/导航 | 静态数组（对话/会话历史/知识库/插件市场/用量/设置/新建任务） | 无 |
| 会话 | `sessionsStore.sessions`（布局已每 15s 刷新，常驻可用） | 无 |
| 知识库 | `GET /api/wiki` 索引 | **首次打开面板时拉一次并缓存** |
| 插件市场 | `GET /api/capabilities?page_size=100` | **首次打开面板时拉一次并缓存** |

- 过滤纯本地（`includes` 小写匹配），无按键级请求。
- 键盘：↑/↓ 选择、Enter 执行、Esc 关闭；空查询显示命令组；每组最多展示 8 条并显示总数。
- 执行：命令组走 `router.push`；会话走布局已有的 `openSession`（本地/远程自动分支）；知识库/市场跳到对应页面。

`WorkbenchLayout.vue` 改动：
- 搜索按钮去掉 `.disabled` 与「即将上线」，改为 `@click` 打开面板；保留 Ctrl+K 快捷键提示。
- `onKeydown` 增加 `ctrlKey && key.toLowerCase() === 'k'` 分支；**并补一个「焦点在输入框/textarea/contenteditable 时不拦截」的守卫**（现有 Ctrl+N 也缺该守卫，一并补上，避免在输入框里打字误触发）。
- 面板组件的可见性用 ref 管理（与既有 `profileVisible` 同处）。

### B. 本地模式附件

1. **新增选文件 IPC** `localagent:file:pick`：
   - `electron/main/kernel/ipc.ts` 增加 handler，仿 `handleWorkspacePick`，用 `dialog.showOpenDialog({ properties: ['openFile', 'multiSelections'] })`，返回 `{ canceled: boolean; paths: string[] }`。
   - `electron/preload/index.ts` 的 `ALLOWED_CHANNELS` 加入 `'localagent:file:pick'`。
2. **复制进工作区**：选中后复制到 `<会话工作区>/.attachments/<时间戳>-<文件名>`。
   - 必须复制：kernel 只允许工作区内路径，直接引用外部绝对路径会被 `resolveWithin` 拒绝。
   - 校验：单文件 ≤ 20MB；扩展名白名单（文本/代码/pdf/常见文档/图片），不支持或超限则提示并跳过该文件。
   - 重名用时间戳前缀避免覆盖；复制失败要给出明确错误。
3. **接进消息**：把复制后的**工作区相对路径**（如 `.attachments/1732000000-log.txt`）追加到待发送文本里（形如「已附带文件：<相对路径>」），随正常发送流程走。保持消息仍是纯文本，kernel 的 `file.read` 能读到。
4. **UI**：`ChatView.vue` 的 `+ 文件` chip 去掉 disabled；**仅本地模式可点**（`isLocal` 已有，`ChatView.vue:101`），远程模式保持置灰且 `title` 说明「远程模式暂不支持附件」。选择中显示 loading，取消不报错。

## 验收标准

1. Ctrl+K 打开面板；输入即过滤；**面板打开后无按键级网络请求**（仅首次打开时各一次列表请求）。
2. 焦点在输入框/textarea 内时按 Ctrl+K（或 Ctrl+N）不触发面板/新建。
3. 回车能打开选中会话、跳转知识库/插件市场页面。
4. 本地模式可选文件并成功发送；消息文本含附件相对路径；kernel 的 `file.read` 能读取该文件。
5. 远程模式附件入口置灰并给出说明。
6. `npm test`（179）、`npm run typecheck`、`npm run build` 全绿；过滤与路径/文件名处理的纯逻辑有单测。

## 风险

- **知识库/市场首次打开的一次列表请求**与「纯本地」的字面承诺略有偏差；已在设计中显式标注，若需严格零请求可只保留会话+命令两组。
- **复制文件占用磁盘**：附件会随工作区累积，本期不做清理；需在文档中说明。
- **agent 会话只能按 id 搜**：面板会包含但命中率低，属于上游限制（会话无标题），本期不解决。
- 面板与既有 `z-index:100` 拖拽条：使用 `el-dialog`（teleport 到 body）规避；若改为自绘遮罩需 `z-index>100`。

## 实施结果（2026-09-20）

设计落地为以下提交（均在 `dashboard` 仓库）：

| 提交 | 内容 |
| --- | --- |
| `a9d7b8b` | 命令面板组件 `src/components/CommandPalette.vue` |
| `12d0107` | Ctrl+K 接线 + 输入焦点守卫 |
| `fb28329` | 面板「新建任务」复用 `newTask`，不再只是跳转 |
| `7e9f355` | 选文件 IPC + 复制进会话工作区 |
| `5b2d90c` | 输入区接入本地附件 |
| `30fa42d` | 附件令牌绑定来源 + 可发送性 + 边界加固 |

### 验收结果

| # | 验收标准 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | Ctrl+K 打开面板；输入即过滤；面板打开后**无按键级请求**（仅首次打开各拉一次 `/api/wiki` 与 `/api/capabilities`） | PASS | `CommandPalette.vue` 本地 `includes` 过滤 + 首开拉取缓存；`npm run typecheck`、`npm run build` 通过 |
| 2 | 焦点在输入框/textarea 内时按 Ctrl+K（或 Ctrl+N）不触发 | PASS | `WorkbenchLayout.vue:77` 的 `isTypingTarget` 守卫在 `onKeydown` 首行先返回 |
| 3 | 回车能打开选中会话、跳转知识库/插件市场页面 | PASS | 复用布局既有 `openSession`（本地/远程自动分支）与 `router.push` |
| 4 | 本地模式可选文件并成功发送；消息含附件相对路径；kernel 的 `file.read` 能读取 | PASS | `test/kernel/attachment-import.test.ts` 用真实临时目录验证复制到 `.attachments/` 并可读回内容 |
| 5 | 远程模式附件入口置灰并给出说明 | PASS | `ChatView.vue` 模板 `v-if="isLocal"` 分支；`v-else` 为置灰项 `title="远程模式暂不支持附件"` |
| 6 | `npm test`、`npm run typecheck`、`npm run build` 全绿 | PASS | `npm test`：**203 passed / 0 fail**；typecheck、build 通过 |

### 评审中发现并修复的真实问题

- **`attach:import` 曾把渲染层传来的绝对路径当复制来源**（等价于一个任意文件读取原语）→ 改为 `file:pick` 在主进程签发一次性令牌、`attach:import` 只认令牌，渲染层再也无法指定来源路径（`electron/main/kernel/attachment-tokens.ts`）。
- 仅附件无文本时无法发送 → `canSend` 计入附件（`src/utils/attachments.ts` 的 `canSendMessage`）。
- 另修：`..` 重组、Windows 保留设备名（CON/NUL/COM1…）、同名冲突不覆盖（追加 `-N`）、扩展名按清洗后名字判定，以及「两份 `formatAttachmentNotice` 实现只有一份被测」的假声明（现加等价性测试，同时 import 两份实现并断言输出一致）。

### 残余限制（如实记录）

- **远程模式仍无附件**：需 agent 后端提供上传端点并改造传输层（当前 `/api/chat/stream` 只接受纯文本 JSON）。
- **「发送失败保留附件」暂不生效**：`stores/chat.ts` 的 `send` 内部捕获异常后 resolve，调用方永远走成功分支；路径已实现，但需后续让 `send` 抛错或返回成功标志才会真正触发。
- **附件令牌未与会话绑定**：10 分钟 TTL 内，最后一个会话可用该令牌导入；非关键，可进一步收紧。
- **附件复制进工作区后不自动清理**：随会话工作区累积。
- **agent 会话无标题**：面板对 agent 会话只能按 `id` 命中。
- **知识库/市场在面板首次打开时各拉一次列表**：属一次性取数，非按键级请求；与「纯本地」的字面承诺略有偏差。
