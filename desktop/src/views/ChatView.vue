<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRouter } from 'vue-router'
import { Avatar, ArrowDown, Camera, CaretRight, Check, Clock, Close, Cpu, Document, FolderOpened, Loading, MagicStick, Microphone, Paperclip, Plus, Select, Tools } from '@element-plus/icons-vue'
import ArtifactsPanel from '../components/ArtifactsPanel.vue'
import { useChatStore, type ToolTrace, type UiMessage } from '../stores/chat'
import { useSettingsStore } from '../stores/settings'
import { useSessionsStore, channelLabel } from '../stores/sessions'
import MarkdownBody from '../components/MarkdownBody.vue'
import { canSendMessage, formatAttachmentNotice } from '../utils/attachments'
import { copyToClipboard, markdownToPlainText } from '../utils/clipboard'
import { formatContextUsage } from '../utils/context-usage'
import {
  dataUrlToBytes,
  screenshotFailureMessage,
  screenshotFileName,
  type ScreenshotResult
} from '../utils/screenshot'
import {
  formatDuration,
  transitionVoiceState,
  VOICE_UNCONFIGURED_HINT,
  type VoiceState
} from '../utils/voice'
import { agentApi } from '../api/agent'

const chat = useChatStore()
const settings = useSettingsStore()
const sessionsStore = useSessionsStore()
const input = ref('')
const scrollRef = ref<HTMLElement | null>(null)
const expandedTools = ref<Set<string>>(new Set())
const elapsed = ref(0)
let elapsedTimer: number | undefined
/** 已导入工作区、待随下一条消息发送的附件 */
const pendingAttachments = ref<{ relPath: string; name: string; thumb?: string }[]>([])
const attaching = ref(false)
const dragging = ref(false)
/** 产物面板(Canvas)开关 */
const artifactsOpen = ref(false)

/** 当前在线会话是否为"只读渠道"(钉钉等外部渠道): 仅可查看, 不可在 dashboard 续聊 */
const readOnlyChannel = computed(
  () =>
    chat.sessionMode === 'agent' &&
    !!chat.sessionId &&
    !!chat.currentChannelKind &&
    chat.currentChannelKind !== 'web'
)
const readOnlyChannelLabel = computed(() => channelLabel(chat.currentChannelKind))

const canSend = computed(
  () => !readOnlyChannel.value && canSendMessage(input.value, pendingAttachments.value.length, chat.streaming)
)

async function send() {
  if (!canSend.value) return
  const staged = pendingAttachments.value
  let text = input.value
  // 附件以相对路径文本并入消息，kernel 的 file_read 即可读取
  if (staged.length) text += formatAttachmentNotice(staged.map((a) => a.relPath))
  input.value = ''
  try {
    await chat.send(text)
    // 仅发送成功才清空待发附件：失败时保留，避免丢失已导入文件
    if (staged.length) pendingAttachments.value = []
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

/** 顶部快捷: 新建对话(清空当前会话, 保持当前模式) */
function newChat(): void {
  if (chat.streaming) return
  chat.newSession()
  input.value = ''
}

/** 顶部快捷: 打开会话历史页 */
function openHistory(): void {
  void router.push('/sessions')
}

function removeAttachment(relPath: string) {
  pendingAttachments.value = pendingAttachments.value.filter((a) => a.relPath !== relPath)
}

/** 用令牌走导入(本地)/上传(在线), 并把结果并入待发 chip */
async function stageByToken(token: string) {
  const res = isLocal.value
    ? await window.desktop.invoke<{
        imported: { relPath: string; name: string; thumb?: string }[]
        skipped: { path: string; reason: string }[]
      }>('localagent:attach:import', { sessionId: chat.sessionId, token })
    : await window.desktop.invoke<{
        imported: { relPath: string; name: string; thumb?: string }[]
        skipped: { path: string; reason: string }[]
      }>('agent:attach:upload', { token })
  if (res.imported.length) pendingAttachments.value = [...pendingAttachments.value, ...res.imported]
  if (res.skipped.length) {
    const detail = res.skipped.map((s) => `${s.path}:${s.reason}`).join('；')
    ElMessage.warning(`已跳过 ${res.skipped.length} 个文件：${detail}`)
  }
}

/** 选文件并以当前模式导入/上传;附件暂存为可移除 chip,随下一条消息发送 */
async function pickAttachments() {
  if (attaching.value || chat.streaming) return
  if (isLocal.value && !chat.sessionId) {
    ElMessage.warning('请先选择工作区创建本地会话')
    return
  }
  attaching.value = true
  try {
    const picked = await window.desktop.invoke<{ canceled: boolean; token?: string }>('localagent:file:pick')
    if (!picked || picked.canceled || !picked.token) return
    await stageByToken(picked.token)
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    attaching.value = false
  }
}

/** 剪贴板图片(Ctrl+V): 主进程落盘后走与选文件相同的导入/上传流程 */
async function onPaste(e: ClipboardEvent) {
  const items = Array.from(e.clipboardData?.items ?? [])
  const imageItem = items.find((it) => it.type.startsWith('image/'))
  if (!imageItem) return // 纯文本粘贴交回默认行为
  e.preventDefault()
  if (attaching.value || chat.streaming) return
  if (isLocal.value && !chat.sessionId) {
    ElMessage.warning('请先选择工作区创建本地会话')
    return
  }
  const file = imageItem.getAsFile()
  if (!file) return
  attaching.value = true
  try {
    const bytes = new Uint8Array(await file.arrayBuffer())
    const ext = (file.type.split('/')[1] || 'png').replace('jpeg', 'jpg')
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
    const { token } = await window.desktop.invoke<{ token: string; name: string }>('localagent:file:paste', {
      name: `pasted-${stamp}.${ext}`,
      bytes
    })
    await stageByToken(token)
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    attaching.value = false
  }
}

/** 拖拽文件到输入区: 读字节走与粘贴相同的落盘+导入/上传流程 */
async function onDrop(e: DragEvent) {
  dragging.value = false
  if (attaching.value || chat.streaming) return
  const files = Array.from(e.dataTransfer?.files ?? []).filter((f) => f.size > 0)
  if (!files.length) return
  if (isLocal.value && !chat.sessionId) {
    ElMessage.warning('请先选择工作区创建本地会话')
    return
  }
  attaching.value = true
  try {
    for (const file of files) {
      const bytes = new Uint8Array(await file.arrayBuffer())
      const { token } = await window.desktop.invoke<{ token: string; name: string }>('localagent:file:paste', {
        name: file.name || `dropped-${Date.now()}.bin`,
        bytes
      })
      await stageByToken(token)
    }
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    attaching.value = false
  }
}

/**
 * 截图提问(E): 主进程整屏捕获(MVP) → dataURL 解码 → 走与粘贴图片一致的令牌通路。
 * 失败(无权限/无屏幕)由主进程给出可读文案, 这里直接展示。
 */
async function captureScreenshot(): Promise<void> {
  if (attaching.value || chat.streaming) return
  if (isLocal.value && !chat.sessionId) {
    ElMessage.warning('请先选择工作区创建本地会话')
    return
  }
  attaching.value = true
  try {
    const res = await window.desktop.invoke<ScreenshotResult>('desktop:screenshot')
    if (!res || res.ok !== true) {
      ElMessage.error(res && res.ok === false ? res.error : screenshotFailureMessage(undefined))
      return
    }
    const bytes = dataUrlToBytes(res.dataUrl)
    const { token } = await window.desktop.invoke<{ token: string; name: string }>('localagent:file:paste', {
      name: screenshotFileName(Date.now()),
      bytes
    })
    await stageByToken(token)
  } catch (err) {
    ElMessage.error(screenshotFailureMessage(err))
  } finally {
    attaching.value = false
  }
}

function plusScreenshot(): void {
  plusOpen.value = false
  void captureScreenshot()
}

/** 窗口聚焦时的 Ctrl+Shift+A 截图快捷键(输入框内也生效) */
function onComposerHotkey(e: KeyboardEvent): void {
  if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'a') {
    e.preventDefault()
    void captureScreenshot()
  }
}

function stop() {
  chat.stopStream()
}

/** 模式切换(下拉):失败时 store 不变,下拉自动回退 */
function onModeChange(mode: 'agent' | 'local') {
  chat
    .switchMode(mode)
    .catch((err: Error) => ElMessage.error(err.message))
}

/** 在线权限模式文案 */
const PERM_MODES: Record<'default' | 'smart' | 'auto', { label: string; hint: string }> = {
  default: { label: '每次询问', hint: '写文件、执行命令前都先询问' },
  smart: { label: '必要时询问', hint: '仅危险操作(删除/高危命令)询问' },
  auto: { label: '完全访问', hint: '不再询问(非管理员在审批联动下会降级为必要时询问)' }
}

/** ---- 加号菜单: 文件 / 模式 / 权限 / 专家 / 技能 / 连接器 ---- */
const router = useRouter()
const plusOpen = ref(false)
const plusPersonas = ref<Array<{ name: string; description?: string }>>([])
const plusSkills = ref<Array<{ name: string; description?: string }>>([])

/** 连接器条目(与主进程 listMcpStatus 对齐): enabled=false 为用户禁用的持久偏好 */
type PlusMcpItem = {
  name: string
  transport: 'stdio' | 'sse' | 'remote'
  status: 'connected' | 'idle'
  /** 本次进程是否已发起过连接尝试(false=尚未触发，首次对话时自动连接) */
  attempted: boolean
  enabled: boolean
}

const plusMcp = ref<PlusMcpItem[]>([])

async function loadPlusMcp(): Promise<void> {
  plusMcp.value = await window.desktop.invoke<PlusMcpItem[]>('localagent:mcp:status').catch(() => [])
}

async function loadPlusData(): Promise<void> {
  plusPersonas.value = await window.desktop
    .invoke<Array<{ name: string; description?: string }>>('localagent:personas:list')
    .catch(() => [])
  plusSkills.value = await window.desktop
    .invoke<Array<{ name: string; description?: string }>>('localagent:skills:list')
    .catch(() => [])
  await loadPlusMcp()
}

/** 连接器状态文案: 禁用优先; 未触发过连接显示"待连接"(首次对话时自动连); 平台网关照旧带前缀 */
function mcpStatusText(m: PlusMcpItem): string {
  if (!m.enabled) return '已禁用'
  const label = m.status === 'connected' ? '已连接' : m.attempted === false ? '待连接' : '未连接'
  return m.transport === 'remote' ? `云端托管 · ${label}` : label
}

function mcpTooltip(m: PlusMcpItem): string {
  if (!m.enabled) return '已禁用（记住），可随时启用'
  if (m.status !== 'connected' && m.attempted === false) {
    return '尚未建立连接：发送一条消息后会按需连接'
  }
  return '本机对话可调用该连接器的工具'
}

/** 点开关切换连接器: 写偏好 → 失效连接缓存(下一轮 chat 生效) → 刷新列表 */
async function toggleMcp(m: PlusMcpItem): Promise<void> {
  try {
    const res = await window.desktop.invoke<{ ok: boolean; name: string; enabled: boolean }>(
      'localagent:mcp:set-enabled',
      { name: m.name, enabled: !m.enabled }
    )
    await loadPlusMcp()
    ElMessage.success(res.enabled ? `已启用连接器 ${res.name}，下一轮对话生效` : `已禁用连接器 ${res.name}`)
  } catch (err) {
    ElMessage.error(`连接器切换失败: ${(err as Error).message}`)
  }
}

watch(plusOpen, (open) => {
  if (open) void loadPlusData()
})

/** ---- 本机连接器环境变量(stdio):覆写包内 ${VAR} 占位符,下次对话生效 ---- */
type ConnectorEnvRow = {
  id: number
  key: string
  /** mcp.json 当前值（留空时保持不变；同时作为 placeholder 提示） */
  origin: string
  /** 用户输入（空=保持不变；删除行=移除该键） */
  input: string
  /** 内核托管（${MARKET_URL}/${MARKET_TOKEN}），只读展示 */
  auto: boolean
  /** 新增行：键名可编辑 */
  isNew: boolean
}

const envDialogOpen = ref(false)
const envDialogName = ref('')
const envRows = ref<ConnectorEnvRow[]>([])
const envDeletedKeys = ref<string[]>([])
const envLoading = ref(false)
const envSaving = ref(false)
let envRowSeq = 0

/** 键名含 secret/token 等字样时用密码框（与 market 详情页同款判断） */
function isSecretKey(key: string): boolean {
  const k = key.toLowerCase()
  return ['password', 'secret', 'token', 'key', 'passwd', 'credential'].some((s) => k.includes(s))
}

/** 值提示：占位符原样展示；敏感键的真实值不回显为明文 placeholder */
function envPlaceholder(row: ConnectorEnvRow): string {
  if (!row.origin) return '变量值'
  if (isSecretKey(row.key) && !/^\$\{[^}]+\}$/.test(row.origin.trim())) return '已配置（留空保持不变）'
  return row.origin
}

/** 打开「设置」对话框：拉取当前 env，autoVars 只读、其余可编辑 */
async function openMcpEnv(m: PlusMcpItem): Promise<void> {
  plusOpen.value = false
  envDialogName.value = m.name
  envDialogOpen.value = true
  envLoading.value = true
  envRows.value = []
  envDeletedKeys.value = []
  try {
    const res = await window.desktop.invoke<{ env: Record<string, string>; autoVars: string[] }>(
      'localagent:mcp:env:get',
      { name: m.name }
    )
    const auto = new Set(res.autoVars ?? [])
    envRows.value = Object.entries(res.env ?? {}).map(([key, value]) => ({
      id: ++envRowSeq,
      key,
      origin: value,
      input: '',
      auto: auto.has(key),
      isNew: false
    }))
  } catch (err) {
    envDialogOpen.value = false
    ElMessage.error(`读取环境变量失败: ${(err as Error).message}`)
  } finally {
    envLoading.value = false
  }
}

function addEnvRow(): void {
  envRows.value.push({ id: ++envRowSeq, key: '', origin: '', input: '', auto: false, isNew: true })
}

/** 删除行：新增行直接移除；已有的自定义键记为待删除（保存时空串提交） */
function removeEnvRow(idx: number): void {
  const [row] = envRows.value.splice(idx, 1)
  if (row && !row.isNew && row.key && !envDeletedKeys.value.includes(row.key)) {
    envDeletedKeys.value.push(row.key)
  }
}

/** 保存：未输入保持原值；空键/空值行跳过；删除键以空串提交（主进程据此移除） */
async function saveMcpEnv(): Promise<void> {
  if (!envDialogName.value) return
  const edited: Record<string, string> = {}
  for (const row of envRows.value) {
    if (row.auto) continue
    const key = row.key.trim()
    if (!key) continue
    const value = row.input.trim() || row.origin
    if (!value) continue
    edited[key] = value
  }
  for (const key of envDeletedKeys.value) {
    if (!(key in edited)) edited[key] = ''
  }
  envSaving.value = true
  try {
    await window.desktop.invoke('localagent:mcp:env:set', { name: envDialogName.value, env: edited })
    envDialogOpen.value = false
    await loadPlusMcp()
    ElMessage.success('已保存，下次对话生效')
  } catch (err) {
    ElMessage.error(`保存失败: ${(err as Error).message}`)
  } finally {
    envSaving.value = false
  }
}

function plusPickFile(): void {
  plusOpen.value = false
  void pickAttachments()
}

/** ---- 模型选择(仅本地会话): 会话级优先, 缺省跟随系统默认 ---- */
const modelOpen = ref(false)
const modelOptions = ref<string[]>([])
const modelDefault = ref('')
const modelError = ref('')
let modelLoaded = false

/** 当前会话实际使用的模型 */
const currentModel = computed(() => {
  const own = sessionsStore.localSessions.find((s) => s.id === chat.sessionId)?.model ?? ''
  return own.trim() || settings.config.localModel
})

async function loadModels(): Promise<void> {
  if (modelLoaded) return
  try {
    const res = await window.desktop.invoke<{ models: string[]; defaultModel: string; error?: string }>(
      'localagent:models:list'
    )
    modelOptions.value = res.models ?? []
    modelDefault.value = res.defaultModel ?? ''
    modelError.value = res.error ?? ''
    modelLoaded = true
  } catch (err) {
    modelError.value = (err as Error).message
  }
}

watch(modelOpen, (open) => {
  if (open) void loadModels()
})

/** 切换当前会话模型(写回会话 meta, 下一轮生效) */
async function pickModel(model: string): Promise<void> {
  modelOpen.value = false
  if (!chat.sessionId || model === currentModel.value) return
  try {
    const meta = await window.desktop.invoke<{ id: string; model: string } | null>('localagent:sessions:setModel', {
      id: chat.sessionId,
      model
    })
    if (!meta) {
      ElMessage.error('切换模型失败: 会话不存在或模型无效')
      return
    }
    const hit = sessionsStore.localSessions.find((s) => s.id === meta.id)
    if (hit) hit.model = meta.model
    ElMessage.success(`已切换模型: ${meta.model}`)
  } catch (err) {
    ElMessage.error(`切换模型失败:${(err as Error).message}`)
  }
}


/** ---- 上下文用量指示(E): 本地按 buildContext 预算, 在线显示只读模型徽标 ---- */
interface ContextUsage {
  used: number
  budget: number
  messages: number
  total: number
  model: string
}

const contextUsage = ref<ContextUsage | null>(null)
const onlineModel = ref('')

const contextView = computed(() =>
  contextUsage.value ? formatContextUsage(contextUsage.value.used, contextUsage.value.budget) : null
)
const contextTooltip = computed(() => {
  const u = contextUsage.value
  const view = contextView.value
  if (!u || !view) return ''
  const parts = [
    `模型:${u.model || '未设置'}`,
    `已用 ${u.used} / 预算 ${u.budget} 字符（${view.percent}%）`,
    `上下文消息 ${u.messages} 条`
  ]
  if (u.total > u.used) parts.push(`历史共 ${u.total} 字符, 超出预算部分已截断`)
  return parts.join('\n')
})

/** 拉取本地会话上下文用量(切换会话/流式结束/消息变化时刷新) */
async function loadContextUsage(): Promise<void> {
  if (!isLocal.value || !chat.sessionId) {
    contextUsage.value = null
    return
  }
  try {
    contextUsage.value = await window.desktop.invoke<ContextUsage>('localagent:context:usage', {
      sessionId: chat.sessionId
    })
  } catch {
    // 用量属只读增强, 拉取失败不打断对话
    contextUsage.value = null
  }
}

/** 在线会话只读模型徽标: agent /api/agent/status 的 model(无 usage 事件可用的降级展示) */
async function loadOnlineModel(): Promise<void> {
  if (isLocal.value || onlineModel.value) return
  try {
    const status = await agentApi.status()
    onlineModel.value = String((status as { model?: unknown } | null)?.model ?? '').trim()
  } catch {
    // 拉不到模型名时不展示徽标
  }
}

watch(
  () => [chat.sessionId, chat.streaming, chat.messages.length],
  () => {
    void loadContextUsage()
  }
)

/** 切回在线模式且尚未取到模型名时补拉一次(只读徽标, 失败静默) */
watch(
  () => chat.sessionMode,
  (mode) => {
    if (mode !== 'local') void loadOnlineModel()
  }
)

/** 当前生效的工作区: 会话已选 > 默认工作空间 */
const effectiveWorkspace = computed(() => chat.localWorkspace || settings.config.defaultWorkspace || '')
/** 路径取目录名(工作区展示用) */
function workspaceName(path: string): string {
  return (path || '').split(/[\\/]/).filter(Boolean).pop() ?? ''
}

/**
 * 本地工作空间: 列出最近使用 / 新建(用户目录下) / 打开本地文件夹
 */
const wsOpen = ref(false)
const wsItems = ref<Array<{ path: string; name: string; lastUsedAt: number }>>([])

async function loadWorkspaces(): Promise<void> {
  const res = await window.desktop
    .invoke<{ items: Array<{ path: string; name: string; lastUsedAt: number }> }>('localagent:workspace:list')
    .catch(() => ({ items: [] }))
  wsItems.value = res.items ?? []
}

watch(wsOpen, (open) => {
  if (open) void loadWorkspaces()
})

/** 切到某个工作空间(开新本地会话); 默认工作区为空时顺手记下 */
async function wsUse(path: string): Promise<void> {
  wsOpen.value = false
  try {
    await chat.startLocalSession(undefined, path)
    ElMessage.success(`已切换到工作空间「${workspaceName(path)}」`)
    if (!settings.config.defaultWorkspace) await settings.save({ defaultWorkspace: path })
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

/** 新建工作空间: 在用户目录 <home>/AI 工作空间/<名字> 下创建并切换 */
async function wsCreate(): Promise<void> {
  wsOpen.value = false
  try {
    const { value } = await ElMessageBox.prompt('给工作空间起个名字', '新建工作空间', {
      inputPattern: /\S+/,
      inputErrorMessage: '名字不能为空',
      inputPlaceholder: '例如:设备运维',
      confirmButtonText: '创建',
      cancelButtonText: '取消'
    })
    const item = await window.desktop.invoke<{ path: string; name: string }>('localagent:workspace:create', {
      name: value
    })
    await chat.startLocalSession(undefined, item.path)
    ElMessage.success(`已创建并切换到「${item.name}」`)
    if (!settings.config.defaultWorkspace) await settings.save({ defaultWorkspace: item.path })
  } catch (err) {
    if (err !== 'cancel') ElMessage.error((err as Error).message)
  }
}

/** 打开本地文件夹作为工作空间 */
async function wsOpenFolder(): Promise<void> {
  wsOpen.value = false
  try {
    const pick = await window.desktop.invoke<{ canceled: boolean; path?: string }>('localagent:workspace:pick')
    if (!pick || pick.canceled || !pick.path) return
    await chat.startLocalSession(undefined, pick.path)
    ElMessage.success(`已切换到工作空间「${workspaceName(pick.path)}」`)
    if (!settings.config.defaultWorkspace) await settings.save({ defaultWorkspace: pick.path })
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

/** 用市场安装的 agent 人设开一个本地会话 */
async function plusUsePersona(name: string): Promise<void> {
  plusOpen.value = false
  try {
    await chat.startLocalSession(name)
    // 输入框为空时给出该专家的起手模板,与参考产品一致
    if (!input.value.trim()) input.value = `请以「${name}」身份帮我处理：`
    ElMessage.success(`已用「${name}」开启本地会话`)
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

/** 技能以 /名字 前缀触发(kernel 只认行首 /name) */
function plusUseSkill(name: string): void {
  plusOpen.value = false
  input.value = `/${name} ${input.value}`.trimEnd() + ' '
  activeSkill.value = name
}

/** 当前生效的技能(输入框行首的 /name);用户手动删掉前缀即自动解除 */
const activeSkill = ref('')
watch(input, (val) => {
  const m = val.match(/^\/([\w-]+)\s/)
  activeSkill.value = m ? m[1] : ''
})

function clearSkill(): void {
  if (!activeSkill.value) return
  input.value = input.value.replace(new RegExp(`^/${activeSkill.value}\\s*`), '')
  activeSkill.value = ''
}

/** 退出当前人设(新建一个不带人设的本地会话) */
async function clearPersona(): Promise<void> {
  try {
    await chat.startLocalSession()
    ElMessage.success('已切回默认人设')
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

/** 推荐/常用问答标签:点一下填进输入框(复用空态那组建议) */
function useSuggestion(text: string): void {
  input.value = text
}

const permMode = computed(() => settings.permissionMode)
const permLabel = computed(() => PERM_MODES[permMode.value]?.label ?? '每次询问')

/** 顶部标题: 在线用会话标题(支持重命名), 本地用工作区目录名 */
const headTitle = computed(() => {
  if (isLocal.value) {
    const ws = effectiveWorkspace.value
    const base = ws.split(/[\\/]/).filter(Boolean).pop() ?? ''
    return base ? `本地 · ${base}` : '本地会话'
  }
  if (!chat.sessionId) return '新会话'
  const item = sessionsStore.sessions.find((s) => s.id === chat.sessionId)
  return item?.title || chat.sessionId.split(':').slice(2).join(':') || '会话'
})



function onPermissionModeChange(mode: 'default' | 'smart' | 'auto') {
  settings.setPermissionMode(mode).catch((err: Error) => ElMessage.error(err.message))
  // 本地内核同样生效(每次询问/必要时询问/完全访问)
  void window.desktop.invoke('localagent:permission:mode', { mode }).catch(() => {})
}

onMounted(() => {
  // 启动时把已保存的权限模式同步给本地内核
  void window.desktop.invoke('localagent:permission:mode', { mode: settings.permissionMode }).catch(() => {})
  // 截图快捷键(窗口聚焦时生效)与上下文/模型徽标首刷
  window.addEventListener('keydown', onComposerHotkey)
  void loadContextUsage()
  void loadOnlineModel()
})

/** 回答权限确认(local 三选项 / online 动态选项) */
function respond(decision: string) {
  chat.respondPermission(decision).catch((err: Error) => ElMessage.error(err.message))
}

/** 运行中的工具(尚无结果) */
function pendingTools(m: UiMessage): ToolTrace[] {
  return m.tools.filter((t) => t.result === undefined)
}

/** 已完成工具(结果已回填) */
function finishedTools(m: UiMessage): ToolTrace[] {
  return m.tools.filter((t) => t.result !== undefined)
}

/** 当前正在执行的工具(最后一个没有结果的 trace) */
function runningTool(m: UiMessage): ToolTrace | undefined {
  const pending = pendingTools(m)
  return pending.length ? pending[pending.length - 1] : undefined
}


/** 聚合视图:同名同类的工具合并为一条(×N),避免「子代理 设备运维」刷满一屏 */
interface ToolGroup {
  key: string
  name: string
  kind: ToolTrace['kind']
  count: number
  items: ToolTrace[]
}

function toolGroups(m: UiMessage): ToolGroup[] {
  const map = new Map<string, ToolGroup>()
  for (const t of finishedTools(m)) {
    const key = `${m.id}-${t.kind}-${t.name}`
    const group = map.get(key) ?? { key, name: t.name, kind: t.kind, count: 0, items: [] }
    group.count += 1
    group.items.push(t)
    map.set(key, group)
  }
  return [...map.values()]
}

function isLastMsg(m: UiMessage): boolean {
  return chat.messages[chat.messages.length - 1] === m
}

/** ---- 消息级操作: 复制整条 / 重新生成 / 编辑重发(仅本地会话) ---- */
const editingId = ref('')
const editText = ref('')

/** 复制(纯文本): 助手消息剥离 Markdown 语法, 用户消息原文 */
async function copyMessage(msg: UiMessage): Promise<void> {
  const text = msg.role === 'assistant' ? markdownToPlainText(msg.content) : msg.content
  const ok = await copyToClipboard(text)
  if (ok) ElMessage.success('已复制')
  else ElMessage.error('复制失败,请检查剪贴板权限')
}

/** 复制 Markdown(原文): 仅助手消息 */
async function copyMarkdown(msg: UiMessage): Promise<void> {
  const ok = await copyToClipboard(msg.content)
  if (ok) ElMessage.success('已复制 Markdown')
  else ElMessage.error('复制失败,请检查剪贴板权限')
}

/** 进入内联编辑(仅本地用户消息, 流式中不可用) */
function startEdit(msg: UiMessage): void {
  if (chat.streaming || !isLocal.value || msg.role !== 'user') return
  editingId.value = msg.id
  editText.value = msg.content
}

function cancelEdit(): void {
  editingId.value = ''
  editText.value = ''
}

/** 提交编辑: 截断该消息之后并重跑本轮(store 里走 localagent:editAndResend) */
async function submitEdit(msg: UiMessage): Promise<void> {
  const text = editText.value.trim()
  if (!text) {
    ElMessage.warning('消息不能为空')
    return
  }
  editingId.value = ''
  await chat.editAndResend(msg, text)
}

/** 重新生成最后一条回答(仅本地会话; 在线会话不展示该按钮) */
async function regenerate(): Promise<void> {
  if (chat.streaming || !isLocal.value) return
  await chat.regenerate()
}

function toggleTool(key: string) {
  const next = new Set(expandedTools.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expandedTools.value = next
}

function onInputKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}

watch(
  () => [chat.messages.length, chat.messages[chat.messages.length - 1]?.content],
  () => {
    void nextTick(() => {
      const el = scrollRef.value
      if (el) el.scrollTop = el.scrollHeight
    })
  }
)

// 流式期间显示"工作中 Xs"
watch(
  () => chat.streaming,
  (on) => {
    if (elapsedTimer) {
      window.clearInterval(elapsedTimer)
      elapsedTimer = undefined
    }
    if (on) {
      elapsed.value = 0
      elapsedTimer = window.setInterval(() => {
        elapsed.value += 1
      }, 1000)
    }
  }
)

/** ---- 语音输入(E): MediaRecorder 录音 → 主进程转写 → 文本插入输入框 ---- */
const asrConfigured = computed(() => (settings.config.asrUrl ?? '').trim().length > 0)
const voiceState = ref<VoiceState>('idle')
const voiceElapsedMs = ref(0)
const voiceElapsedLabel = computed(() => formatDuration(voiceElapsedMs.value))
const voiceTitle = computed(() => {
  if (!asrConfigured.value) return VOICE_UNCONFIGURED_HINT
  if (voiceState.value === 'transcribing') return '正在转写…'
  if (voiceState.value === 'recording') return '点击结束并转写'
  return '语音输入(点击开始录音)'
})

let voiceRecorder: MediaRecorder | null = null
let voiceStream: MediaStream | null = null
let voiceChunks: Blob[] = []
let voiceTimer: number | undefined
let voiceStartedAt = 0
/** 正在 await getUserMedia: 同步互斥, 防止悬挂期第二次点击再取一条流(旧流永不释放) */
let voiceStarting = false
/** 组件已卸载: getUserMedia resolve 后立即释放轨道, 不再建 recorder */
let voiceDisposed = false
/** 转写代数: 取消/新的转写会 +1, 回调据此丢弃过期结果 */
let voiceTranscribeSeq = 0
let voiceTranscribeRequestId = ''

function stopVoiceTimer(): void {
  if (voiceTimer) {
    window.clearInterval(voiceTimer)
    voiceTimer = undefined
  }
}

/** 释放麦克风轨道与计时器(录音结束/取消/组件卸载共用) */
function releaseVoiceStream(): void {
  stopVoiceTimer()
  voiceStream?.getTracks().forEach((track) => track.stop())
  voiceStream = null
}

async function startVoice(): Promise<void> {
  if (voiceStarting || voiceDisposed) return
  voiceStarting = true
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    // 卸载竞态: 组件已销毁则立即停轨, 不建立 recorder
    if (voiceDisposed) {
      stream.getTracks().forEach((track) => track.stop())
      return
    }
    voiceStream = stream
    voiceChunks = []
    const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : 'audio/webm'
    const recorder = new MediaRecorder(stream, { mimeType: mime })
    voiceRecorder = recorder
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) voiceChunks.push(e.data)
    }
    recorder.onstop = () => {
      void finishVoice()
    }
    recorder.start()
    voiceState.value = transitionVoiceState(voiceState.value, 'start')
    voiceStartedAt = Date.now()
    voiceElapsedMs.value = 0
    voiceTimer = window.setInterval(() => {
      voiceElapsedMs.value = Date.now() - voiceStartedAt
    }, 250)
  } catch (err) {
    releaseVoiceStream()
    voiceRecorder = null
    ElMessage.error(`无法访问麦克风：${(err as Error).message}`)
  } finally {
    voiceStarting = false
  }
}

function stopVoice(): void {
  if (voiceState.value !== 'recording') return
  voiceState.value = transitionVoiceState(voiceState.value, 'stop')
  try {
    voiceRecorder?.stop()
  } catch {
    // stop 失败时 onstop 不会触发, 手动落回 idle 避免卡在转写态
    releaseVoiceStream()
    voiceRecorder = null
    voiceState.value = transitionVoiceState(voiceState.value, 'fail')
  }
}

function cancelVoice(): void {
  if (voiceState.value !== 'recording') return
  const recorder = voiceRecorder
  voiceRecorder = null
  if (recorder) {
    recorder.onstop = null
    try {
      recorder.stop()
    } catch {
      // 已停止时忽略
    }
  }
  voiceChunks = []
  releaseVoiceStream()
  voiceState.value = transitionVoiceState(voiceState.value, 'cancel')
  ElMessage.info('已取消录音')
}

/** 录音停止后: 组装 webm → IPC 转写 → 追加到输入框(失败保留原文) */
async function finishVoice(): Promise<void> {
  releaseVoiceStream()
  voiceRecorder = null
  const chunks = voiceChunks
  voiceChunks = []
  if (!chunks.length) {
    voiceState.value = transitionVoiceState(voiceState.value, 'fail')
    ElMessage.warning('没有录到声音,请重试')
    return
  }
  const seq = ++voiceTranscribeSeq
  const requestId = `asr-${Date.now().toString(36)}-${seq}`
  voiceTranscribeRequestId = requestId
  try {
    const blob = new Blob(chunks, { type: chunks[0].type || 'audio/webm' })
    const bytes = new Uint8Array(await blob.arrayBuffer())
    const res = await window.desktop.invoke<{ ok: boolean; text?: string; error?: string }>(
      'desktop:asr:transcribe',
      { bytes, name: 'voice.webm', mime: blob.type || 'audio/webm', requestId }
    )
    // 期间已取消(或已开始新一次转写): 丢弃过期结果
    if (seq !== voiceTranscribeSeq) return
    if (!res || res.ok !== true || !res.text) {
      voiceState.value = transitionVoiceState(voiceState.value, 'fail')
      ElMessage.error((res && res.error) || '语音转写失败')
      return
    }
    const existing = input.value.trimEnd()
    input.value = existing ? `${existing} ${res.text}` : res.text
    voiceState.value = transitionVoiceState(voiceState.value, 'done')
  } catch (err) {
    if (seq !== voiceTranscribeSeq) return
    voiceState.value = transitionVoiceState(voiceState.value, 'fail')
    ElMessage.error(`语音转写失败：${(err as Error).message}`)
  } finally {
    if (voiceTranscribeRequestId === requestId) voiceTranscribeRequestId = ''
  }
}

/** 取消转写(E): 作废结果 + 通知主进程 abort(主进程另有超时兜底) */
function cancelTranscribe(): void {
  if (voiceState.value !== 'transcribing') return
  voiceTranscribeSeq += 1
  if (voiceTranscribeRequestId) {
    void window.desktop.invoke('desktop:asr:abort', { requestId: voiceTranscribeRequestId }).catch(() => {})
    voiceTranscribeRequestId = ''
  }
  voiceState.value = transitionVoiceState(voiceState.value, 'cancel')
  ElMessage.info('已取消转写')
}

function toggleVoice(): void {
  if (!asrConfigured.value) {
    ElMessage.warning(VOICE_UNCONFIGURED_HINT)
    return
  }
  // 启动悬挂期内的重复触发直接忽略(避免第二条流覆盖第一条)
  if (voiceStarting) return
  if (voiceState.value === 'idle') {
    if (chat.streaming) return
    void startVoice()
  } else if (voiceState.value === 'recording') {
    // 录音中即使回答流式进行也必须允许停止(否则麦克风无法收尾)
    stopVoice()
  }
}

/** 组件卸载时丢弃未结束的录音, 不留后台麦克风 */
function disposeVoice(): void {
  voiceDisposed = true
  if (voiceRecorder) {
    voiceRecorder.onstop = null
    try {
      voiceRecorder.stop()
    } catch {
      // 已停止时忽略
    }
    voiceRecorder = null
  }
  voiceChunks = []
  releaseVoiceStream()
}

onBeforeUnmount(() => {
  if (elapsedTimer) window.clearInterval(elapsedTimer)
  window.removeEventListener('keydown', onComposerHotkey)
  disposeVoice()
})

const suggestions = [
  '帮我查一下报销制度的关键条款',
  '把上周的会议纪要整理成待办清单',
  '写一个周报模板,包含进度/风险/下周计划',
  '总结这个项目的当前进展和遗留风险'
]

const localSuggestions = [
  '看看这个项目的目录结构,梳理模块划分',
  '把代码里的 TODO 注释整理成清单',
  '跑一下测试,总结失败原因',
  '解释 src/stores/chat.ts 的实现思路'
]

const isLocal = computed(() => chat.sessionMode === 'local')
</script>

<template>
  <div class="chat-view">
    <div class="chat-col">
    <!-- 本地模式标题区:模式胶囊 + 工作区路径 -->
    <header class="chat-head">
      <span class="chat-title" :title="headTitle">{{ headTitle }}</span>
      <span
        v-if="isLocal && chat.localEphemeral"
        class="chat-tag"
        title="退出后自动删除，不落历史"
      >临时</span>
      <div class="chat-head-actions">
        <button
          class="head-icon-btn"
          title="新建对话"
          :disabled="chat.streaming"
          @click="newChat"
        >
          <el-icon :size="15"><Plus /></el-icon>
        </button>
        <button class="head-icon-btn" title="会话历史" @click="openHistory">
          <el-icon :size="15"><Clock /></el-icon>
        </button>
        <button
          class="head-icon-btn"
          :class="{ active: artifactsOpen }"
          title="产物"
          @click="artifactsOpen = !artifactsOpen"
        >
          <el-icon :size="15"><Document /></el-icon>
        </button>
      </div>
    </header>

    <div ref="scrollRef" class="chat-scroll">
      <template v-if="chat.messages.length === 0">
        <div class="chat-empty">
          <template v-if="isLocal">
            <h1>本地模式</h1>
            <p>我可以读写文件、执行命令、检索代码,直接在工作区帮你干活。</p>
            <p class="empty-ws" :title="effectiveWorkspace">工作区:{{ effectiveWorkspace || '未选择' }}</p>
            <p v-if="!settings.hasUser" class="empty-hint">请先完成企业 SSO 登录(本地模式需要算力网关 apikey)。</p>
          </template>
          <template v-else>
            <h1>有什么可以帮你?</h1>
            <p>我是公司数字员工,可以回答问题、查知识、写文档、跑任务。</p>
            <p v-if="!settings.hasUser" class="empty-hint">提示:若 agent 开启了认证,请先到「设置」登录。</p>
          </template>

          <div class="suggestions">
            <button v-for="s in (isLocal ? localSuggestions : suggestions)" :key="s" class="suggestion" @click="input = s">
              {{ s }}
            </button>
          </div>
        </div>
      </template>

      <template v-for="msg in chat.messages" :key="msg.id">
        <!-- 用户消息:右对齐主色气泡(与零号员工端一致); 悬浮显示 复制/编辑 -->
        <div v-if="msg.role === 'user'" class="msg user">
          <div v-if="editingId === msg.id" class="bubble edit-bubble">
            <el-input
              v-model="editText"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 10 }"
              resize="none"
              class="edit-input"
              @keydown.ctrl.enter="submitEdit(msg)"
              @keydown.esc="cancelEdit"
            />
            <div class="edit-actions">
              <span class="edit-hint">Ctrl+Enter 重发</span>
              <button class="msg-action" @click="cancelEdit">取消</button>
              <button class="msg-action primary" @click="submitEdit(msg)">发送</button>
            </div>
          </div>
          <template v-else>
            <div v-if="!chat.streaming" class="msg-actions">
              <button class="msg-action" title="复制整条消息" @click="copyMessage(msg)">复制</button>
              <button v-if="isLocal" class="msg-action" title="编辑并重发(本地会话)" @click="startEdit(msg)">编辑</button>
            </div>
            <div class="bubble">{{ msg.content }}</div>
          </template>
        </div>

        <!-- AI 消息:卡片气泡内的时间线(思考 / 工具 chip / 正文 / 汇总),与零号员工端一致 -->
        <div v-else class="msg assistant">
          <div class="assistant-col">
          <div class="bubble">
            <!-- 思考内容: 默认折叠, 点开查看(不自动展开) -->
            <details v-if="msg.reasoning" class="reason">
              <summary>思考过程</summary>
              <div class="reason-text">{{ msg.reasoning }}</div>
            </details>

            <!-- 子代理执行输出:实时流式展示,跑完自动收起 -->
            <details v-if="msg.subagentOutput" class="subagent-live" :open="msg.subagentRunning">
              <summary>
                <span v-if="msg.subagentRunning" class="chip-running">●</span>
                {{ msg.subagentRunning ? '子代理执行中…' : '子代理输出' }}
              </summary>
              <div class="subagent-live-text">{{ msg.subagentOutput }}</div>
            </details>

            <MarkdownBody v-if="msg.content" :content="msg.content" />

            <!-- 工具调用: 始终在正文之后(气泡最底部), 折叠为一行; 运行中带动效 -->
            <details v-if="msg.tools.length" class="tools-fold">
              <summary>
                <el-icon v-if="runningTool(msg)" class="is-loading" :size="12"><Loading /></el-icon>
                <el-icon v-else :size="12"><Tools /></el-icon>
                <span class="tools-fold-text">
                  {{ runningTool(msg) ? `正在执行 ${runningTool(msg)?.name}` : `已调用 ${msg.tools.length} 次工具` }}
                </span>
                <span v-if="runningTool(msg)" class="tools-fold-live">运行中</span>
              </summary>
              <div class="tools-fold-body">
                <span v-for="g in toolGroups(msg)" :key="g.key" class="tool-chip done" :class="g.kind"
                      :title="(g.items[g.items.length - 1].result || '').split('\n')[0].slice(0, 120)"
                      @click="toggleTool(g.key)">
                  <span class="tool-name mono">{{ g.name }}</span>
                  <span v-if="g.count > 1" class="tool-count">×{{ g.count }}</span>
                </span>
                <pre v-for="g in toolGroups(msg)" v-show="expandedTools.has(g.key)" :key="'r-' + g.key"
                     class="tool-result">{{ g.items.map((t) => t.result).join('\n---\n') }}</pre>
              </div>
            </details>

            <!-- 大模型运行指示: 生成/等待期间持续动效 -->
            <div v-if="isLastMsg(msg) && chat.streaming" class="gen-pill">
              <el-icon class="is-loading" :size="13"><Loading /></el-icon>
              <span>{{ msg.content ? '大模型生成中' : '等待模型响应' }}{{ elapsed ? ` · ${elapsed}s` : '' }}</span>
              <span class="gen-dots"><i /><i /><i /></span>
            </div>

            <el-alert v-if="msg.error" :title="msg.error" type="error" :closable="false" class="err" />
          </div>
          <!-- 悬浮操作条: 复制(纯文本) / 复制 Markdown 原文 / 重新生成(仅本地最后一条) -->
          <div v-if="!chat.streaming" class="msg-actions">
            <button class="msg-action" title="复制为纯文本" @click="copyMessage(msg)">复制</button>
            <button class="msg-action" title="复制 Markdown 原文" @click="copyMarkdown(msg)">复制 Markdown</button>
            <button
              v-if="isLocal && isLastMsg(msg)"
              class="msg-action"
              title="重新生成(本地会话; 在线会话暂不支持)"
              @click="regenerate()"
            >
              重新生成
            </button>
          </div>
          </div>
        </div>
      </template>
    </div>

    <!-- ZCode 式输入区:大圆角容器,工具栏内嵌 -->
    <footer class="composer-wrap">
      <!-- 推荐/常用问答标签(点一下填进输入框) -->
      <div class="suggest-row">
        <button v-for="s in (isLocal ? localSuggestions : suggestions)" :key="s" class="suggest-chip" :disabled="chat.streaming" @click="useSuggestion(s)">
          {{ s }}
        </button>
      </div>

      <div
        class="composer"
        :class="{ dragging, readonly: readOnlyChannel }"
        @paste="onPaste"
        @dragover.prevent="dragging = true"
        @dragleave="dragging = false"
        @drop.prevent="onDrop"
      >
        <!-- 只读渠道提示: 钉钉等外部会话仅可查看, 不能在此续聊 -->
        <div v-if="readOnlyChannel" class="composer-readonly">
          <el-icon :size="14"><FolderOpened /></el-icon>
          <span>该会话来自{{ readOnlyChannelLabel }}，仅支持查看历史；请回到钉钉继续对话。</span>
        </div>
        <!-- 待发附件:可单个移除,发送时并入消息文本 -->
        <div v-if="pendingAttachments.length" class="pending-attachments">
          <span v-for="a in pendingAttachments" :key="a.relPath" class="attach-chip" :title="a.relPath">
            <img v-if="a.thumb" class="attach-thumb" :src="a.thumb" :alt="a.name" />
            <span class="attach-name">{{ a.name }}</span>
            <el-icon class="attach-remove" @click="removeAttachment(a.relPath)"><Close /></el-icon>
          </span>
        </div>
        <el-input
          v-model="input"
          type="textarea"
          :autosize="{ minRows: 3, maxRows: 12 }"
          :disabled="readOnlyChannel"
          :placeholder="readOnlyChannel ? '该会话只读，请回到钉钉继续对话' : '今天帮你做些什么？@ 引用对话文件，/ 调用技能'"
          resize="none"
          class="composer-input"
          @keydown="onInputKeydown"
        />
        <div class="composer-bar">
          <div class="composer-left">
            <!-- 加号菜单: 文件 / 专家 / 技能 / 连接器(模式与权限在框外下方) -->
            <el-popover
              v-model:visible="plusOpen"
              trigger="click"
              placement="top-start"
              :width="340"
              :show-arrow="false"
              popper-class="plus-popper"
            >
              <template #reference>
                <button class="plus-btn" :disabled="chat.streaming || readOnlyChannel" title="添加文件 / 专家 / 技能 / 连接器">
                  <el-icon :size="15"><Plus /></el-icon>
                </button>
              </template>

              <div class="plus-menu">
                <button class="plus-item" @click="plusPickFile">
                  <el-icon :size="14"><Paperclip /></el-icon>
                  <span class="plus-label">添加文件</span>
                  <span class="plus-sub">粘贴/拖拽也可以</span>
                </button>

                <button class="plus-item" @click="plusScreenshot">
                  <el-icon :size="14"><Camera /></el-icon>
                  <span class="plus-label">截图</span>
                  <span class="plus-sub">Ctrl+Shift+A · 截取整个屏幕</span>
                </button>


                <div class="plus-group">专家(市场安装的 agent)</div>
                <p v-if="!plusPersonas.length" class="plus-empty">
                  还没有安装专家, 去<button class="plus-link" @click="plusOpen = false; router.push('/market')">能力市场</button>安装
                </p>
                <button v-for="p in plusPersonas" :key="p.name" class="plus-item" @click="plusUsePersona(p.name)">
                  <span class="plus-label">{{ p.name }}</span>
                  <span class="plus-sub">{{ p.description || '' }}</span>
                </button>

                <div class="plus-group">技能</div>
                <p v-if="!plusSkills.length" class="plus-empty">还没有技能, 可在能力市场安装</p>
                <button v-for="s in plusSkills" :key="s.name" class="plus-item" @click="plusUseSkill(s.name)">
                  <span class="plus-label mono">/{{ s.name }}</span>
                  <span class="plus-sub">{{ s.description || '' }}</span>
                </button>

                <div class="plus-group">连接器</div>
                <p v-if="!plusMcp.length" class="plus-empty">还没有连接器, 可在能力市场安装</p>
                <div
                  v-for="m in plusMcp"
                  :key="m.name"
                  class="mcp-row"
                  :class="{ 'mcp-off': !m.enabled }"
                >
                  <button
                    class="plus-item"
                    :title="mcpTooltip(m)"
                    @click="toggleMcp(m)"
                  >
                    <span class="mcp-dot" :class="m.enabled ? m.status : 'off'" />
                    <span class="plus-label">{{ m.name }}</span>
                    <span class="plus-sub">{{ mcpStatusText(m) }}</span>
                    <span class="mcp-toggle" :class="{ on: m.enabled }" />
                  </button>
                  <button
                    v-if="m.transport === 'stdio'"
                    class="mcp-env-btn"
                    type="button"
                    title="设置环境变量（本机运行用，可覆写包内 ${VAR} 占位符）"
                    @click="openMcpEnv(m)"
                  >设置</button>
                </div>
                <button class="plus-item" @click="plusOpen = false; router.push('/market')">
                  <el-icon :size="14"><MagicStick /></el-icon>
                  <span class="plus-label">管理专家 / 技能 / 连接器</span>
                </button>
              </div>
            </el-popover>

            <!-- 当前生效的专家人设 / 技能(可移除) -->
            <span v-if="isLocal && chat.localPersona" class="active-chip persona">
              <el-icon :size="12"><Avatar /></el-icon>
              {{ chat.localPersona }}
              <el-icon class="active-x" :size="11" title="切回默认人设" @click="clearPersona"><Close /></el-icon>
            </span>
            <span v-if="activeSkill" class="active-chip skill">
              <span class="mono">/{{ activeSkill }}</span>
              <el-icon class="active-x" :size="11" title="移除技能" @click="clearSkill"><Close /></el-icon>
            </span>

            <!-- 上下文用量(E): 本地会话按 buildContext 字符预算, ≥80% 变告警色 -->
            <span
              v-if="isLocal && contextView"
              class="ctx-badge"
              :class="contextView.level"
              :title="contextTooltip"
            >
              上下文 {{ contextView.label }}
            </span>

            <span class="composer-hint">
              {{ attaching ? (isLocal ? '正在导入…' : '正在上传…') : 'Enter 发送 · / 调用技能 · Shift+Enter 换行' }}
            </span>
          </div>
          <div class="composer-right">
            <!-- 模型选择(仅本地会话): 会话级模型, 缺省跟随系统默认 -->
            <el-popover
              v-if="isLocal"
              v-model:visible="modelOpen"
              trigger="click"
              placement="top-end"
              :width="260"
              :show-arrow="false"
              popper-class="model-popper"
            >
              <template #reference>
                <button class="model-btn" :disabled="chat.streaming" :title="`当前模型:${currentModel || '未设置'}`">
                  <el-icon :size="14"><Cpu /></el-icon>
                  <span class="model-name">{{ currentModel || '模型' }}</span>
                  <el-icon :size="11"><ArrowDown /></el-icon>
                </button>
              </template>
              <div class="model-menu">
                <p v-if="modelError" class="model-err">模型列表获取失败:{{ modelError }}</p>
                <button
                  v-for="m in modelOptions"
                  :key="m"
                  class="model-item"
                  :class="{ on: m === currentModel }"
                  @click="pickModel(m)"
                >
                  <span class="mono model-item-name">{{ m }}</span>
                  <span v-if="m === modelDefault" class="model-tag">默认</span>
                  <el-icon v-if="m === currentModel" :size="13"><Check /></el-icon>
                </button>
                <p v-if="!modelOptions.length && !modelError" class="model-empty">暂无可用模型</p>
                <p class="model-foot">仅影响当前本地会话; 新会话使用设置页的默认模型</p>
              </div>
            </el-popover>
            <!-- 在线会话: 只读模型徽标(agent 侧模型; 流结束事件暂无 usage 的降级展示) -->
            <span v-if="!isLocal && onlineModel" class="model-badge" title="云端 Agent 模型（只读）">
              {{ onlineModel }}
            </span>
            <!-- 语音输入(E): 录音/转写中显示计时与取消; 未配置 ASR 时禁用 -->
            <span v-if="voiceState === 'recording'" class="voice-timer" title="录音中">
              <span class="voice-dot" />{{ voiceElapsedLabel }}
              <button class="voice-cancel" title="取消录音" @click="cancelVoice">取消</button>
            </span>
            <span v-else-if="voiceState === 'transcribing'" class="voice-timer">
              转写中…
              <button class="voice-cancel" title="取消转写" @click="cancelTranscribe">取消</button>
            </span>
            <button
              class="icon-btn voice-btn"
              :class="{ 'voice-active': voiceState === 'recording' }"
              :disabled="!asrConfigured || voiceState === 'transcribing' || readOnlyChannel"
              :title="voiceTitle"
              @click="toggleVoice"
            >
              <el-icon :size="15">
                <Loading v-if="voiceState === 'transcribing'" class="is-loading" />
                <Microphone v-else />
              </el-icon>
            </button>
            <button v-if="!chat.streaming" class="send-btn" :disabled="!canSend" title="发送" @click="send">
              <el-icon :size="15"><CaretRight /></el-icon>
            </button>
            <button v-else class="send-btn stop" title="停止" @click="stop">
              <span class="stop-square" />
            </button>
          </div>
        </div>
      </div>

      <!-- 框外底部: 模式 / 权限 选择(与输入框分离) -->
      <div class="composer-foot">
        <div class="foot-left">
          <el-dropdown trigger="click" :disabled="chat.streaming" @command="onModeChange">
            <button class="foot-chip" :disabled="chat.streaming">
              {{ isLocal ? '本地模式' : '在线 · 零号员工' }}
              <span class="foot-caret">▾</span>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="agent">
                  在线 · 零号员工
                  <el-icon v-if="!isLocal" class="mode-check"><Check /></el-icon>
                </el-dropdown-item>
                <el-dropdown-item command="local">
                  本地模式
                  <el-icon v-if="isLocal" class="mode-check"><Check /></el-icon>
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>

          <el-popover
            v-if="isLocal"
            v-model:visible="wsOpen"
            trigger="click"
            placement="top-start"
            :width="320"
            :show-arrow="false"
            popper-class="plus-popper"
          >
            <template #reference>
              <button class="foot-chip" :disabled="chat.streaming" :title="effectiveWorkspace || '未选择工作区'">
                工作区 · {{ workspaceName(effectiveWorkspace) || '未选择' }}
                <span
                  v-if="effectiveWorkspace && effectiveWorkspace === settings.config.defaultWorkspace"
                  class="foot-tag"
                >默认</span>
                <span class="foot-caret">▾</span>
              </button>
            </template>

            <div class="plus-menu">
              <div class="plus-group">工作空间</div>
              <p v-if="!wsItems.length" class="plus-empty">还没有工作空间记录</p>
              <button v-for="w in wsItems" :key="w.path" class="plus-item" :title="w.path" @click="wsUse(w.path)">
                <el-icon :size="14"><FolderOpened /></el-icon>
                <span class="plus-label">{{ w.name }}</span>
                <span v-if="w.path === settings.config.defaultWorkspace" class="foot-tag">默认</span>
                <span class="plus-sub">{{ w.path }}</span>
              </button>
              <div class="plus-divider" />
              <button class="plus-item" @click="wsCreate">
                <el-icon :size="14"><Plus /></el-icon>
                <span class="plus-label">新建工作空间</span>
                <span class="plus-sub">在用户目录下创建</span>
              </button>
              <button class="plus-item" @click="wsOpenFolder">
                <el-icon :size="14"><FolderOpened /></el-icon>
                <span class="plus-label">打开本地文件夹</span>
                <span class="plus-sub">用已有目录作为工作空间</span>
              </button>
            </div>
          </el-popover>

          <el-dropdown trigger="click" :disabled="chat.streaming" @command="onPermissionModeChange">
            <button class="foot-chip" :disabled="chat.streaming" :title="PERM_MODES[permMode].hint">
              权限 · {{ permLabel }}
              <span class="foot-caret">▾</span>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-for="(info, key) in PERM_MODES" :key="key" :command="key">
                  {{ info.label }}
                  <el-icon v-if="permMode === key" class="mode-check"><Check /></el-icon>
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
        <span class="foot-hint">{{ isLocal ? '本地执行,不经云端' : '由零号员工云端执行' }}</span>
      </div>
    </footer>
    </div>

    <!-- 右侧产物侧边栏(不遮挡对话) -->
    <ArtifactsPanel v-if="artifactsOpen" @close="artifactsOpen = false" />

    <!-- 本地工具权限确认:流结束/停止时由 store 自动收起 -->
    <el-dialog
      :model-value="!!chat.pendingPermission"
      title="权限确认"
      width="480px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :show-close="false"
    >
      <div v-if="chat.pendingPermission" class="perm-body">
        <p v-if="chat.pendingPermission.tool" class="perm-tool">工具:<b>{{ chat.pendingPermission.tool }}</b></p>
        <pre class="perm-summary">{{ chat.pendingPermission.summary || '(无参数摘要)' }}</pre>
        <p class="perm-hint">
          {{ chat.pendingPermission.mode === 'online' ? '请选择如何处理该请求。' : '该工具会改动你的工作区,请确认是否允许执行。' }}
        </p>
      </div>
      <template #footer>
        <template v-if="chat.pendingPermission?.mode === 'online'">
          <el-button
            v-for="opt in chat.pendingPermission.options?.length ? chat.pendingPermission.options : ['允许', '拒绝']"
            :key="opt"
            :type="opt === '允许' ? 'primary' : opt === '拒绝' ? 'danger' : 'default'"
            :plain="opt !== '允许'"
            @click="respond(opt)"
          >
            {{ opt }}
          </el-button>
        </template>
        <template v-else>
          <el-button type="danger" plain @click="respond('deny')">拒绝</el-button>
          <el-button plain @click="respond('allow_session')">仅本次会话允许</el-button>
          <el-button
            type="warning"
            plain
            title="以后该工具不再询问，可在 设置→本地模式 中清除"
            @click="respond('allow_always')"
          >
            允许并记住此工具
          </el-button>
          <el-button type="primary" @click="respond('allow')">允许</el-button>
        </template>
      </template>
    </el-dialog>

    <!-- 本机连接器环境变量:覆写包内 ${VAR} 占位符, 保存后下次对话生效 -->
    <el-dialog
      v-model="envDialogOpen"
      :title="`环境变量 · ${envDialogName}`"
      width="560px"
      align-center
    >
      <div v-loading="envLoading" class="mcp-env-dialog">
        <p class="mcp-env-hint">
          本机(stdio)连接器启动时注入这些变量, 可把包内 <code>${VAR}</code> 占位符覆写为自己的值。
        </p>
        <p v-if="!envLoading && !envRows.length" class="mcp-env-empty">
          该连接器没有环境变量, 可「添加变量」。
        </p>
        <div v-for="(row, idx) in envRows" :key="row.id" class="mcp-env-row">
          <template v-if="row.auto">
            <span class="mcp-env-key">{{ row.key }}</span>
            <el-input :model-value="row.origin" size="small" disabled class="mcp-env-value" />
            <span class="mcp-env-auto">由系统注入</span>
          </template>
          <template v-else>
            <el-input
              v-if="row.isNew"
              v-model="row.key"
              size="small"
              placeholder="变量名"
              class="mcp-env-key-input"
            />
            <span v-else class="mcp-env-key">{{ row.key }}</span>
            <el-input
              v-model="row.input"
              size="small"
              :type="isSecretKey(row.key) ? 'password' : 'text'"
              :placeholder="envPlaceholder(row)"
              class="mcp-env-value"
            />
            <el-button text size="small" type="danger" @click="removeEnvRow(idx)">删除</el-button>
          </template>
        </div>
        <div>
          <el-button text size="small" @click="addEnvRow">+ 添加变量</el-button>
        </div>
      </div>
      <template #footer>
        <el-button @click="envDialogOpen = false">取消</el-button>
        <el-button type="primary" :loading="envSaving" @click="saveMcpEnv">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* 消息气泡:与零号员工端(agent/frontend/src/style.css)保持一致 */
.msg { margin-bottom: 18px; display: flex; }
.msg.user { justify-content: flex-end; }
.msg .bubble {
  max-width: 80%; border-radius: 12px; padding: 10px 14px;
  line-height: 1.68; background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
}
.msg.user .bubble { background: var(--el-color-primary); color: #fff; border: none; white-space: pre-wrap; }
.msg .tools { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 7px; }
.msg .err { margin-top: 8px; }

/* 消息悬浮操作条(复制/编辑/重新生成): 默认视觉隐藏, 悬停或键盘聚焦时显示。
   用 opacity 而非 display:none/visibility:hidden, 保证按钮始终在 Tab 序列内(可达)。 */
.assistant-col { display: flex; flex-direction: column; max-width: 80%; min-width: 0; }
.assistant-col .bubble { max-width: 100%; }
.msg-actions {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-top: 4px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.12s ease;
}
.msg.user .msg-actions { margin: 0 8px 0 0; align-self: center; }
.msg:hover .msg-actions,
.msg:focus-within .msg-actions { opacity: 1; pointer-events: auto; }
.msg-action {
  display: inline-flex; align-items: center; gap: 4px;
  border: 1px solid var(--el-border-color-lighter); background: var(--el-bg-color);
  color: var(--el-text-color-secondary); border-radius: 999px; padding: 2px 10px;
  font-size: 11px; line-height: 1.7; cursor: pointer;
}
.msg-action:hover { color: var(--el-color-primary); border-color: var(--el-color-primary-light-5); }
.msg-action.primary { color: var(--el-color-primary); border-color: var(--el-color-primary-light-5); }

/* 内联编辑: 用户气泡切换为可编辑卡片 */
.msg.user .bubble.edit-bubble {
  background: var(--el-bg-color); color: var(--el-text-color-primary);
  border: 1px solid var(--el-border-color); white-space: normal;
  display: flex; flex-direction: column; gap: 6px; width: 100%; max-width: 640px;
}
.edit-input :deep(.el-textarea__inner) { box-shadow: none; padding: 4px 0; }
.edit-actions { display: flex; align-items: center; justify-content: flex-end; gap: 6px; }
.edit-hint { margin-right: auto; font-size: 11px; color: var(--el-text-color-secondary); }

/* 会话标题旁的「临时」徽标 */
.chat-tag {
  flex: none; margin-left: 8px; padding: 1px 8px; border-radius: 999px;
  font-size: 11px; line-height: 1.7;
  color: var(--el-color-warning); background: var(--el-color-warning-light-9);
  border: 1px solid var(--el-color-warning-light-7);
}


/* 工具调用: 折叠为一行; 运行中带动效 */
.tools-fold {
  margin: 2px 0 8px;
  font-size: 12px;
}

.tools-fold summary {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border-radius: 999px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-regular);
  cursor: pointer;
  user-select: none;
  list-style: none;
}

.tools-fold summary::-webkit-details-marker {
  display: none;
}

.tools-fold[open] summary {
  background: var(--el-fill-color-darker);
}

.tools-fold-text {
  max-width: 340px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tools-fold-live {
  color: var(--el-color-primary);
  font-size: 11px;
  animation: blink 1.2s infinite;
}

.tools-fold-body {
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
}
.reason summary { font-size: 12px; color: var(--el-text-color-secondary); cursor: pointer; }
.reason-text { font-size: 12px; color: var(--el-text-color-secondary); white-space: pre-wrap; margin-top: 4px; }

.tool-chip {
  display: inline-flex; align-items: center; gap: 5px;
  border: 1px solid #e3b0ff; background: #faf3ff; color: #6b3fa0;
  border-radius: 10px; padding: 1px 8px; font-size: 11px; margin: 1px 2px 1px 0;
  line-height: 1.6;
}
.tool-chip.done { cursor: pointer; }
.tool-chip.done:hover { filter: brightness(0.97); }
.tool-chip.subagent { border-color: #a7c7ff; background: #f2f7ff; color: #2f5fa8; }
.tool-chip .tool-name { font-weight: 600; font-size: 11px; }
.tool-chip .tool-sub { color: #8a6bb5; font-size: 10.5px; }
.tool-chip .tool-count { font-size: 10px; opacity: 0.75; }
.chip-running { color: #f56c6c; font-size: 9px; animation: blink 1s infinite; }
@keyframes blink { 50% { opacity: 0.2; } }
.subagent-live {
  margin: 6px 0; padding: 6px 9px; border-radius: 8px;
  background: var(--el-fill-color-lighter); border: 1px solid var(--el-border-color-lighter);
}
.subagent-live summary { font-size: 11.5px; color: var(--el-text-color-secondary); cursor: pointer; }
.subagent-live-text {
  margin-top: 5px; font-size: 12px; color: var(--el-text-color-regular);
  white-space: pre-wrap; max-height: 220px; overflow: auto;
}
.tool-result-group { margin-top: 2px; }

/* 大模型运行指示: 旋转图标 + 三点跳动 */
.gen-pill {
  margin-top: 8px;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 4px 12px;
  border-radius: 999px;
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-size: 12.5px;
}

.gen-dots {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.gen-dots i {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: currentColor;
  animation: gen-bounce 1.1s infinite ease-in-out;
}

.gen-dots i:nth-child(2) {
  animation-delay: 0.15s;
}

.gen-dots i:nth-child(3) {
  animation-delay: 0.3s;
}

@keyframes gen-bounce {
  0%,
  80%,
  100% {
    transform: translateY(0);
    opacity: 0.45;
  }
  40% {
    transform: translateY(-4px);
    opacity: 1;
  }
}
.proc-hint { color: var(--el-text-color-secondary); font-size: 13px; margin-top: 4px; }
.caret-line { color: var(--el-text-color-secondary); margin-top: 2px; }
.caret { color: var(--el-color-primary); animation: caret-blink 0.9s step-end infinite; }
@keyframes caret-blink { 50% { opacity: 0; } }
.tool-result {
  margin: 6px 0 0; padding: 8px 10px; background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter); border-radius: 8px;
  font-family: ui-monospace, Consolas, monospace; font-size: 12px;
  max-height: 240px; overflow: auto; white-space: pre-wrap;
}
.mono { font-family: ui-monospace, Consolas, monospace; }

/* 上下文用量徽标(E): 本地会话; ≥80% 切换为告警配色 */
.ctx-badge {
  flex: none;
  padding: 1px 9px;
  border-radius: 999px;
  font-size: 11px;
  line-height: 1.7;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  white-space: nowrap;
}
.ctx-badge.warn {
  color: var(--el-color-warning);
  background: var(--el-color-warning-light-9);
  border-color: var(--el-color-warning-light-7);
}

/* 在线会话只读模型徽标(E) */
.model-badge {
  flex: none;
  max-width: 168px;
  padding: 1px 9px;
  border-radius: 999px;
  font-size: 11px;
  line-height: 1.7;
  color: var(--el-text-color-secondary);
  border: 1px dashed var(--el-border-color);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 语音输入(E): 录音计时/取消与录音态按钮 */
.voice-timer {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}
.voice-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--el-color-danger);
  animation: blink 1s infinite;
}
.voice-cancel {
  border: none;
  background: transparent;
  padding: 0;
  font-size: 11.5px;
  color: var(--el-color-primary);
  cursor: pointer;
}
.voice-btn.voice-active {
  color: #fff;
  background: var(--el-color-danger);
}
.voice-btn.voice-active:hover:not(:disabled) {
  color: #fff;
}

/* 连接器行:主按钮占满整行, stdio 连接器尾部追加「设置」入口 */
.mcp-row {
  display: flex;
  align-items: center;
  gap: 2px;
  border-radius: 8px;
}
.mcp-row:hover {
  background: var(--el-fill-color-light);
}
.mcp-row .plus-item {
  flex: 1;
  min-width: 0;
}
.mcp-row .plus-item:hover {
  background: transparent;
}
.mcp-row.mcp-off {
  opacity: 0.6;
}
.mcp-env-btn {
  flex: none;
  border: none;
  background: transparent;
  color: var(--el-color-primary);
  font-size: 12px;
  padding: 4px 8px;
  border-radius: 6px;
  cursor: pointer;
}
.mcp-env-btn:hover {
  background: var(--el-fill-color);
}

/* 环境变量编辑对话框 */
.mcp-env-dialog {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 72px;
}
.mcp-env-hint {
  margin: 0 0 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}
.mcp-env-empty {
  margin: 4px 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.mcp-env-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.mcp-env-key {
  flex: none;
  width: 190px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}
.mcp-env-key-input {
  flex: none;
  width: 190px;
}
.mcp-env-value {
  flex: 1;
  min-width: 0;
}
.mcp-env-auto {
  flex: none;
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
</style>
