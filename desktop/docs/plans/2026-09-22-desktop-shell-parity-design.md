# Dashboard 桌面能力补齐 设计（2026-09-22）

## 背景

员工端已具备聊天/会话/产物/知识库/模型选择等核心能力，但**桌面壳层几乎空白**（仅单实例锁）。
目标：对齐 ChatGPT / Claude 桌面端常规体验，补齐托盘、快捷唤起、自启、通知、自动更新与若干
对话核心操作。基线参考：ChatGPT Desktop（托盘/菜单栏、Alt+Space 快速提问、开机自启、
自动更新、通知、语音、截图）、Claude Desktop（快速输入、MCP 连接器、Artifacts）。

## 现状盘点（证据）

| 能力 | 现状 |
|---|---|
| 单实例 | ✅ `requestSingleInstanceLock`（`electron/main/index.ts`） |
| 托盘 / 原生菜单 / 全局快捷键 / 自启 / 通知 / 自动更新 | ❌ 全缺（electron/ 下 0 命中） |
| 多窗口 / 深链协议 / 截图 / 语音 | ❌（语音仅占位按钮） |
| 消息级：重新生成 / 编辑重发 / 复制整条 | ❌（仅代码块复制） |
| 会话：置顶/改名/搜索/删除(逻辑) | ✅；归档/导出/临时会话 ❌ |
| 快捷键 | Ctrl+K 命令面板、Ctrl+N 新会话（`WorkbenchLayout.vue:115-130`） |

## 阶段划分

### A. 桌面壳基础包（本批先做）
1. **系统托盘**：`Tray`（图标复用 `build/icon.png`，`nativeImage.resize(16/32)`）；
   菜单：打开主界面 / 新建会话 / 退出；左键单击切换显示；关闭按钮默认**最小化到托盘**
   （设置项 `closeToTray`，默认 true；首次隐藏时托盘气泡提示一次）。
2. **全局快捷键快速提问**：默认 `Alt+Space`（设置项 `quickHotkey`，可改/可关）；
   唤起无边框置顶小窗（新路由 `#/quick`）：输入框 + Enter 发送；发送后**创建新会话并发送**
   （走当前默认模式与默认模型），主窗口聚焦到该会话；Esc/失焦关闭小窗。
   - 注册失败（被占用）→ 设置页提示，不阻断启动。
3. **开机自启**：设置项 `launchAtLogin`（默认关）；`app.setLoginItemSettings({openAtLogin})`。
4. **系统通知**：流式回答**结束时**且主窗口未聚焦 → 系统通知（标题=会话名，正文=回答摘要），
   点击聚焦并跳转该会话；设置项 `notifyOnFinish`（默认开）、`notifySound`（默认关，`shell.beep`）。

### B. 自动更新（内网分发）
- `electron-updater` + `generic` provider；更新源地址放 `enterprise.json` 字段
  `updateFeedUrl`（未配置 → 「检查更新」提示未配置，不报错）。
- 启动后延迟静默检查（可关），设置页「关于」加入「检查更新」按钮与状态显示；
  下载完成提示「重启安装」。
- 发布流程：`npm run dist:win` 产物（exe + `latest.yml`）上传到内网静态目录（IT 提供地址）。

#### B1. 配置与行为
- `AppConfig.autoCheckUpdate`（默认 `true`，设置页「关于」可关）：启动后延迟 **30s**
  静默 `checkForUpdates`，失败只体现在状态里，不打断使用；保存后下次启动生效。
- `AppConfig.updateFeedUrl`（默认 `''`，企业级字段）：由 `enterprise.json` 注入（示例
  `"updateFeedUrl": "https://ai.xzrobot.com/updates/dashboard"`）；留空则功能降级为
  「未配置更新源」，不发任何网络请求。设置页「关于」**只读展示**该地址（是否已配置 +
  原文），不提供编辑入口。
- 写入权限：`config:set` 走**键白名单**（`theme/localModel/defaultWorkspace/permissionMode/`
  `closeToTray/quickHotkey/launchAtLogin/notifyOnFinish/notifySound/autoCheckUpdate`），
  OIDC、服务地址、`agentServiceToken`、`updateFeedUrl` 等企业/敏感字段从渲染层传入一律
  忽略并告警；seed 与主进程内部仍走 `updateConfig` 直写。
- 更新源校验：`setFeedURL` 前用 `new URL()` 校验，必须可解析、协议为 `http:`/`https:` 且
  host 非空；非法地址 → 状态「更新源地址非法」，不设置源、不排检查、不发起网络请求。
- 事件 → 状态文案：未配置 / 尚未检查 / 检查中 / 已是最新 / 发现新版本 x.y.z /
  已下载待安装；失败文案折叠并截断（≤120 字）。
- 客户端下载：`autoDownload = true`；下载完成后系统通知
  「新版本 x.y.z 已下载，点击重启安装」，点击通知或设置页「重启安装」调
  `quitAndInstall`；未下载完成时拒绝安装（避免误退出）。
- **`autoInstallOnAppQuit = false`**：显式关闭「退出即静默安装」，安装只经
  `quitAndInstall` 一条路径，与「点击重启安装」文案一致。

#### B2. 发布步骤（IT/运维）
1. 改 `package.json` 的 `version` 并提交，在 `dashboard/` 执行 `npm run dist:win`，
   产物在 `release/<version>/`：
   - `Dashboard Setup <version>.exe`（安装包）
   - `Dashboard Setup <version>.exe.blockmap`（增量更新）
   - `latest.yml`（更新元数据，electron-updater 据此比对版本/校验）
2. 将上述 3 个文件上传到内网静态目录（地址由 IT 提供，需允许匿名 GET），保持文件名不变：
   `https://ai.xzrobot.com/updates/dashboard/`；新版本覆盖 `latest.yml`，
   **不要**让 CDN/代理长期缓存 `latest.yml`。
3. **发布前**把该地址写入随包分发的 `build/enterprise.json` 的 `updateFeedUrl`
   （模板见 `build/enterprise.example.json`）**再执行打包**——`build/enterprise.json` 是
   打包时注入的运行时配置，只改源码仓库而不重打安装包不会生效；老机器升级到含该字段的
   版本后，首次启动 seed 会自动补注入（字段缺失才注入，不覆盖用户已保存值）。
   - 当前仓库的 `build/enterprise.json`（gitignored）尚未写入 `updateFeedUrl`，因此现有
     安装包会显示「未配置更新源」；待内网地址确定后补写并重新执行 `npm run dist:win`。
4. 验证：目标机器启动 30s 后（或设置页手动「检查更新」）应显示「发现新版本 x.y.z」，
   下载完成后提示重启安装；把 `latest.yml` 的版本回退到与客户端相同应显示「已是最新版本」。
- `electron-builder.yml` 里的 `publish` 是占位源（`https://updates.invalid/dashboard`），
  只为打包时产出 `latest.yml`/`app-update.yml`；运行期一律用配置的 `updateFeedUrl` 覆盖
  （`autoUpdater.setFeedURL({ provider: 'generic', url })`）。

### C. 消息级操作
- **复制整条消息**：两种模式通用。悬浮操作条「复制」（纯文本，助手消息剥离 Markdown 语法）+
  助手消息「复制 Markdown」（原文，markdown-it 渲染前内容）。
- **重新生成**（仅本地会话；**在线会话不提供**，UI 隐藏且主进程只接受本地会话 id）：
  截断到最后一条用户消息（含）之后，复用该消息与既有 history 重跑（不重复追加用户消息），
  复用既有流式事件/streamId 机制。
- **编辑并重发**（仅本地会话）：`index` 为「第几条用户消息」（存储里夹着 assistant/tool 消息，
  与 UI 数组下标不同，UI 侧换算后传入）；替换该条文本并删除其后全部消息后重跑。
- 存储层：`rewriteMessages`（原子重写 JSONL：临时文件 + rename，刷新 updatedAt）与
  `truncateMessages`；**同会话并发守卫（同步 claim）先于任何破坏性写**，守卫命中时存储零改动。
- 中止语义：工具执行前/后检查 abort，已中止不执行/不 emit/不落盘，半截工具轮整体不落盘；
  `buildContext` 额外过滤孤儿 tool 消息（纵深防御，避免 provider 400）。
- 技能消息（`/skill ...`）**落盘原文**，技能正文仅在请求组装时注入（`applySkillsToMessages`），
  保证 UI 所见即存储、重跑可复现。

### D. 会话组织
- **归档**：本地维护归档 ID 集合（`archived.json`，两种模式通用），侧栏分组「已归档」折叠；
  归档不删除，可恢复。
- **导出**：本地会话直接读本地存储；在线会话拉 `/api/agent/sessions/history` 消息后导出
  Markdown / JSON（保存对话框）；标题优先取列表命中项，未命中回退渲染层传入的已知标题。
- **临时会话**：`ephemeral: true`，仅本地模式；运行内正常落盘（本次会话可回看），
  应用启动与退出（`before-quit`）时整体清理，切换会话不删；UI 有「临时」徽标与
  tooltip「退出后自动删除，不落历史」；归档/导出对临时会话禁用（UI + 主进程双层）。

### E. 上下文 / 截图 / 语音
- **上下文用量指示**：输入区显示当前会话上下文占用（本地按 `buildContext` 的字符预算与已用
  估算，徽标 `上下文 12.3k / 96k`，tooltip 展示模型/百分比/消息条数与截断说明，≥80% 告警色）；
  在线会话 agent 流事件无 usage，只读展示 `agent /api/agent/status` 的模型名徽标。
- **截图提问**：`Ctrl+Shift+A`（本地快捷键，窗口聚焦时）或输入区「+」菜单「截图」触发
  （本期无托盘项）→ `desktopCapturer` 截主屏整屏 → 经 `file:paste` 令牌通路作为附件插入
  输入框（MVP 整屏；区域选择/多屏选择后续再做）。
- **语音输入**：`MediaRecorder` 录音 + 可配置 ASR 端点（`enterprise.json` 的 `asrUrl`，
  OpenAI 兼容 `/v1/audio/transcriptions`）；未配置时按钮禁用并提示「未配置语音服务」；
  转写默认 30s 超时，转写中可取消（渲染层作废结果 + IPC abort 透传）。
  （ASR 服务本身不在本批范围；`model` 字段不透传，依赖服务端默认模型。）

## 关键设计决定

- 所有新设置项进 `AppConfig`（本地持久化）并在设置页可改；企业级默认值可经
  `enterprise.json` 注入（沿用既有 seed 机制，扩字段即可）。
- 托盘/通知/快捷窗都在主进程实现；渲染层只加 `#/quick` 路由与设置项 UI。
- 快捷键策略：全局仅 `quickHotkey`（默认 Alt+Space）；窗口内快捷键保持渲染层处理。
- 更新源、ASR 地址都走配置，不硬编码内网地址；未配置时功能降级且可见提示。

## 测试与验证

- 单测（tsx --test）：设置项读写、快捷键注册降级、通知摘要截断、归档集合、导出序列化、
  临时会话退出清理、轮次守卫（claim）顺序、中止不落工具结果/孤儿消息过滤、截图附件装配。
- 手工验收：托盘菜单与关闭到托盘、Alt+Space 唤起并发送、自启（重启系统验证一次）、
  通知点击跳转、检查更新（内网地址就绪后）、重新生成/编辑重发、归档/导出/临时会话、
  上下文指示、截图附件。
- 打包：`npm run dist:win` 产物含托盘图标与 `latest.yml`（B 阶段）。

## 交付顺序

A（托盘/快捷键/自启/通知）→ B（自动更新）→ C（消息级）→ D（会话组织）→ E（上下文/截图/语音）。
每阶段独立提交与验收，安装包在 A/B/E 完成后各重打一次。
