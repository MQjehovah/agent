import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { agentApi } from '../api/agent'
import type {
  ChatStreamEvent,
  HistoryMessage,
  LocalSessionMeta,
  LocalStoredMessage,
  ToolEventData
} from '../api/types'
import { useSettingsStore } from './settings'
import { useSessionsStore, channelKindFromId } from './sessions'
import { shouldNotifyStreamFinish, streamInterrupted } from '../utils/stream'

export interface ToolTrace {
  name: string
  kind: 'tool' | 'subagent' | 'round'
  result?: string
  ts: number
}

export interface UiMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  reasoning: string
  /** 在线模式: 子代理执行期间的流式输出(实时展示, 结束后收起) */
  subagentOutput: string
  /** 子代理是否正在执行 */
  subagentRunning: boolean
  tools: ToolTrace[]
  error: string
  /** 在线流非预期中断(连接掉线, 未收到 done/error): 用于展示「重试」入口 */
  interrupted?: boolean
  ts: number
}

/** 权限确认/反问弹窗数据: 本地 permission_request 或在线 agent ask 事件 */
export interface PendingPermission {
  /** 'local' → localagent:permissions-respond; 'online' → POST /api/chat/answer */
  mode: 'local' | 'online'
  requestId: string
  tool: string
  summary: string
  /** 在线 ask 的可选项(如 ["允许","拒绝"]) */
  options?: string[]
}

/**
 * 单个会话(对话)的运行时状态。
 *
 * 多会话并跑: 每个会话各持一份 bucket, 后台会话的流仍持续把 token/工具轨迹写入
 * 自己的 messages; 切换只改「当前会话」指针, 不打断任何在跑会话。
 */
export interface Conversation {
  id: string
  mode: 'agent' | 'local'
  channelKind: string
  workspace: string
  persona: string
  ephemeral: boolean
  messages: UiMessage[]
  streaming: boolean
  pendingPermission: PendingPermission | null
  error: string
  /** 本轮流开始时间戳(「工作中 Xs」计时用), 0=未在跑 */
  startedAt: number
  updatedAt: number
}

function makeConversation(id: string, patch: Partial<Conversation> = {}): Conversation {
  return {
    id,
    mode: 'agent',
    channelKind: '',
    workspace: '',
    persona: '',
    ephemeral: false,
    messages: [],
    streaming: false,
    pendingPermission: null,
    error: '',
    startedAt: 0,
    updatedAt: Date.now(),
    ...patch
  }
}

let seq = 0
function nextId(prefix: string): string {
  seq += 1
  return `${prefix}-${Date.now().toString(36)}-${seq}`
}

/**
 * 生成服务端认可的会话 ID。
 *
 * agent 侧只接受 `web:{uid}:{rand}` 形态（续聊写回仅允许 `web:` 前缀会话，
 * 其它前缀会被判定为钉钉等外部渠道而拒绝续聊），uid 取自 /api/auth/me。
 */
function newSessionId(uid: string): string {
  const hex = Array.from(crypto.getRandomValues(new Uint8Array(4)))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
  return `web:${uid}:${hex}`
}

function traceLabel(data: ToolEventData, kind: ToolTrace['kind']): string {
  if (kind === 'subagent') return `子代理 ${data.agent_name || data.name || ''}`.trim()
  return String(data.name || 'tool')
}

function resultText(result: unknown): string {
  if (result == null) return ''
  if (typeof result === 'string') return result
  try {
    return JSON.stringify(result, null, 2)
  } catch {
    return String(result)
  }
}

/** 本地工具结果可能很大(整文件读取),轨迹里截断展示 */
function truncateText(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + '…' : s
}

function blankMessage(role: 'user' | 'assistant'): UiMessage {
  return {
    id: nextId(role === 'user' ? 'u' : 'a'),
    role,
    content: '',
    reasoning: '',
    subagentOutput: '',
    subagentRunning: false,
    tools: [],
    error: '',
    ts: Date.now()
  }
}

/** 会话展示标题(找不到时回退默认名),系统通知标题用 */
function sessionTitle(id: string): string {
  try {
    return useSessionsStore().sessions.find((s) => s.id === id)?.title || '零号员工'
  } catch {
    return '零号员工'
  }
}

/**
 * 流式结束通知:把会话/摘要交给主进程,由主进程按 notifyOnFinish 与
 * 主窗口焦点决定是否弹系统通知(渲染层不做焦点判断)。
 */
function notifyStreamFinish(sessionId: string, summary: string): void {
  if (!sessionId || !summary.trim()) return
  void window.desktop
    .invoke('desktop:notify:finish', { sessionId, title: sessionTitle(sessionId), summary })
    .catch(() => {})
}

export const useChatStore = defineStore('chat', () => {
  // ===== state =====
  /** agent 侧用户 uid(拼 web:{uid}:{rand} 会话 ID 用,登录后缓存) */
  const agentUid = ref('')
  /** 当前查看的会话 id('' = 空态/新建草稿) */
  const activeId = ref('')
  /** 所有已载入会话的运行时状态(含后台在跑会话) */
  const conversations = ref<Record<string, Conversation>>({})

  // ===== 并发流的控制句柄(不可序列化, 不进 state) =====
  /** 在线会话进行中的请求控制器, 按会话 id 隔离 */
  const onlineAborts = new Map<string, AbortController>()
  /** 本地会话进行中的流: streamId(停止) + unsub(摘除监听), 按会话 id 隔离 */
  const localStreams = new Map<string, { streamId: string; unsub: () => void }>()

  // ===== 内部助手 =====
  const activeConversation = (): Conversation | null => conversations.value[activeId.value] ?? null

  function ensureConversation(id: string, patch: Partial<Conversation> = {}): Conversation {
    if (!conversations.value[id]) {
      conversations.value[id] = makeConversation(id, patch)
    } else if (Object.keys(patch).length) {
      Object.assign(conversations.value[id], patch)
    }
    // 返回响应式代理: 后台流的写入必须能触发依赖(直接改原始对象不会通知 computed)
    return conversations.value[id]
  }
  const ensureActive = (): Conversation => ensureConversation(activeId.value)
  const setActive = (id: string): void => {
    activeId.value = id
  }

  /** 保留的会话桶上限: 超出后按「最久未更新」淘汰非当前、非在跑的会话(可再按需重新加载) */
  const MAX_KEPT_CONVERSATIONS = 24
  function pruneConversations(): void {
    const entries = Object.entries(conversations.value)
    if (entries.length <= MAX_KEPT_CONVERSATIONS) return
    const prunable = entries
      .filter(([id, c]) => id !== activeId.value && id !== '' && !c.streaming)
      .sort((a, b) => a[1].updatedAt - b[1].updatedAt)
    let excess = entries.length - MAX_KEPT_CONVERSATIONS
    for (const [id] of prunable) {
      if (excess <= 0) break
      delete conversations.value[id]
      excess -= 1
    }
  }

  // ===== 可写视图字段: 代理到「当前会话」, 后台会话各自独立 =====
  // 用可写 computed 保持既有调用方/测试可直接读写顶层字段的语义。
  const sessionId = computed<string>({
    get: () => activeId.value,
    set: (v) => {
      if (v !== activeId.value) activeId.value = v
      if (v) ensureConversation(v)
    }
  })
  const sessionMode = computed<'agent' | 'local'>({
    get: () => activeConversation()?.mode ?? 'agent',
    set: (v) => {
      ensureActive().mode = v
    }
  })
  const currentChannelKind = computed<string>({
    get: () => activeConversation()?.channelKind ?? '',
    set: (v) => {
      ensureActive().channelKind = v
    }
  })
  const localWorkspace = computed<string>({
    get: () => activeConversation()?.workspace ?? '',
    set: (v) => {
      ensureActive().workspace = v
    }
  })
  const localPersona = computed<string>({
    get: () => activeConversation()?.persona ?? '',
    set: (v) => {
      ensureActive().persona = v
    }
  })
  const localEphemeral = computed<boolean>({
    get: () => activeConversation()?.ephemeral ?? false,
    set: (v) => {
      ensureActive().ephemeral = v
    }
  })
  const messages = computed<UiMessage[]>({
    get: () => activeConversation()?.messages ?? [],
    set: (v) => {
      ensureActive().messages = v
    }
  })
  const streaming = computed<boolean>({
    get: () => activeConversation()?.streaming ?? false,
    set: (v) => {
      ensureActive().streaming = v
    }
  })
  const pendingPermission = computed<PendingPermission | null>({
    get: () => activeConversation()?.pendingPermission ?? null,
    set: (v) => {
      ensureActive().pendingPermission = v
    }
  })
  const error = computed<string>({
    get: () => activeConversation()?.error ?? '',
    set: (v) => {
      ensureActive().error = v
    }
  })
  /** 当前会话本轮开始时间(0=未在跑), 「工作中 Xs」计时用 */
  const activeStartedAt = computed<number>(() => activeConversation()?.startedAt ?? 0)

  // ===== actions =====
  function newSession(): void {
    const blank = conversations.value['']
    if (blank && !blank.streaming) {
      blank.messages = []
      blank.error = ''
      blank.pendingPermission = null
      blank.mode = 'agent'
      blank.channelKind = ''
      blank.workspace = ''
      blank.persona = ''
      blank.ephemeral = false
    }
    activeId.value = ''
  }

  /** 取 agent 侧 uid(优先缓存,失败则查 /api/auth/me);拿不到返回空串 */
  async function resolveAgentUid(): Promise<string> {
    if (agentUid.value) return agentUid.value
    try {
      const me = await agentApi.me()
      const uid = me?.id === undefined || me?.id === null ? '' : String(me.id)
      if (uid) agentUid.value = uid
      return uid
    } catch {
      return ''
    }
  }

  /**
   * 加载在线会话历史:agent 历史是「一条消息一行」(含 role=tool 与多次 assistant),
   * 直接铺平会拆成一堆小气泡;这里按轮次归一化 —— 每条 user 开启一轮,
   * 其后的 assistant/tool 合并进同一个气泡(content 拼接、reasoning 累积、
   * tool_calls 建轨迹占位、tool 结果按 tool_call_id 回填)。
   *
   * 若目标会话正在本地跑(已有 bucket.streaming), 只切换不覆盖, 避免打断实时气泡。
   */
  function loadHistory(id: string, history: HistoryMessage[]): void {
    const existing = conversations.value[id]
    if (existing?.streaming) {
      setActive(id)
      return
    }
    const list = normalizeHistory(history)
    const conv = ensureConversation(id, {
      mode: 'agent',
      channelKind: channelKindFromId(id),
      workspace: '',
      ephemeral: false,
      error: '',
      pendingPermission: null
    })
    conv.messages = list
    conv.updatedAt = Date.now()
    setActive(id)
    pruneConversations()
  }

  /**
   * 新建本地会话:工作区优先级 = 显式指定 > 默认工作区 > 弹窗选择(并记住为默认);
   * 可指定市场安装的人设; options.ephemeral 创建临时会话(退出即删)。
   */
  async function startLocalSession(
    personaName?: string,
    workspaceOverride?: string,
    options: { ephemeral?: boolean } = {}
  ): Promise<void> {
    const settings = useSettingsStore()
    let workspace = (workspaceOverride ?? settings.config.defaultWorkspace ?? '').trim()
    if (!workspace) {
      const pick = await window.desktop.invoke<{ canceled: boolean; path?: string }>('localagent:workspace:pick')
      if (!pick || pick.canceled || !pick.path) return
      workspace = pick.path
      await settings.save({ defaultWorkspace: workspace })
    }
    const meta = await window.desktop.invoke<LocalSessionMeta>('localagent:sessions:create', {
      model: settings.config.localModel,
      workspace,
      personaName: personaName || undefined,
      ephemeral: options.ephemeral === true
    })
    const conv = ensureConversation(meta.id, {
      mode: 'local',
      channelKind: 'local',
      workspace: meta.workspace,
      persona: meta.personaName ?? personaName ?? '',
      ephemeral: Boolean(options.ephemeral)
    })
    conv.messages = []
    conv.error = ''
    conv.pendingPermission = null
    conv.updatedAt = Date.now()
    setActive(meta.id)
    pruneConversations()
  }

  /** 模式切换:切到本地模式会创建(或复用默认工作区)本地会话;切回零号员工回到 agent 空态 */
  async function switchMode(mode: 'agent' | 'local'): Promise<void> {
    if (mode === sessionMode.value) return
    if (mode === 'local') await startLocalSession()
    else newSession()
  }

  /** 加载本地会话历史:assistant.toolCalls 映射为轨迹占位,tool 消息按 toolCallId 回填结果 */
  async function loadLocalMessages(sessionIdArg: string, meta?: { workspace?: string; ephemeral?: boolean }): Promise<void> {
    const existing = conversations.value[sessionIdArg]
    if (existing?.streaming) {
      setActive(sessionIdArg)
      return
    }
    const stored = await window.desktop.invoke<LocalStoredMessage[]>('localagent:messages', sessionIdArg)
    const list = normalizeLocalMessages(stored)
    const conv = ensureConversation(sessionIdArg, {
      mode: 'local',
      channelKind: 'local',
      workspace: meta?.workspace ?? '',
      ephemeral: meta?.ephemeral === true,
      error: '',
      pendingPermission: null
    })
    conv.messages = list
    conv.updatedAt = Date.now()
    setActive(sessionIdArg)
    pruneConversations()
  }

  /** 会话是否正在跑(供侧栏/切换入口判断, 不依赖当前视图) */
  function isRunning(id: string): boolean {
    return conversations.value[id]?.streaming === true
  }

  /** 切到已知会话(不重新加载); 命中返回 true, 未知返回 false(调用方再拉历史) */
  function focus(id: string): boolean {
    if (!id || !conversations.value[id]) return false
    setActive(id)
    return true
  }

  /** 移除会话的本地运行时状态(删除会话/退出时调用); 在跑会话先中止 */
  function dropConversation(id: string): void {
    const conv = conversations.value[id]
    if (conv?.streaming) {
      if (conv.mode === 'local') {
        const s = localStreams.get(id)
        if (s) {
          s.unsub()
          localStreams.delete(id)
          void window.desktop.invoke('localagent:stop', s.streamId).catch(() => {})
        }
      } else {
        onlineAborts.get(id)?.abort()
      }
    }
    delete conversations.value[id]
    if (activeId.value === id) activeId.value = ''
  }

  /** 结束本地流:摘除监听、退出流式态并收起权限弹窗 */
  function finishLocalStream(conv: Conversation): void {
    const s = localStreams.get(conv.id)
    if (s) {
      s.unsub()
      localStreams.delete(conv.id)
    }
    conv.streaming = false
    conv.startedAt = 0
    conv.pendingPermission = null
  }

  function stopStream(): void {
    const conv = activeConversation()
    if (!conv) return
    if (conv.mode === 'local') {
      const s = localStreams.get(conv.id)
      if (s) void window.desktop.invoke('localagent:stop', s.streamId).catch(() => {})
      finishLocalStream(conv)
      return
    }
    onlineAborts.get(conv.id)?.abort()
  }

  async function send(text: string, options: { forceNewSession?: boolean } = {}): Promise<void> {
    const message = text.trim()
    if (!message) return
    if (sessionMode.value === 'local') {
      if (!sessionId.value) {
        error.value = '请先选择工作区创建本地会话'
        return
      }
      await sendLocal(message)
      return
    }

    // 非 web 在线会话(钉钉等)只读: 阻止写回(服务端会 400), 给出明确提示
    if (sessionId.value && currentChannelKind.value && currentChannelKind.value !== 'web') {
      error.value = '该会话来自钉钉等外部渠道，仅支持查看历史，请回到对应渠道继续对话'
      return
    }

    let conv = activeConversation()
    // 无当前会话 / 强制新建 / 无 bucket: 生成新会话(已有在跑会话保留在后台)
    if (!sessionId.value || options.forceNewSession || !conv) {
      const uid = await resolveAgentUid()
      if (!uid) {
        error.value = '无法获取账号信息，请重新登录后再试'
        return
      }
      const id = newSessionId(uid)
      conv = ensureConversation(id, {
        mode: 'agent',
        channelKind: 'web',
        messages: [],
        error: '',
        pendingPermission: null
      })
      setActive(id)
      pruneConversations()
    }
    // 同一会话已在跑: 忽略重复发送(UI 亦会禁用)
    if (conv.streaming) return
    if (conv.channelKind && conv.channelKind !== 'web') return

    const reply = pushTurn(conv, message)
    conv.streaming = true
    conv.startedAt = Date.now()
    conv.error = ''
    const controller = new AbortController()
    onlineAborts.set(conv.id, controller)
    // 到达终态(done/error 事件)后置位; 用于区分「正常结束」与「连接被掐断」
    let terminal = false
    const apply = (event: ChatStreamEvent): void => {
      if (event.type === 'done' || event.type === 'error') terminal = true
      applyEventTo(conv, reply, event)
    }

    let aborted = false
    try {
      const settings = useSettingsStore()
      const outcome = await agentApi.streamChat(
        { message, session_id: conv.id, permission_mode: settings.permissionMode },
        apply,
        controller.signal
      )
      aborted = outcome.aborted
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        conv.error = (err as Error).message
        reply.error = (err as Error).message
      } else {
        aborted = true
      }
    } finally {
      conv.streaming = false
      conv.startedAt = 0
      onlineAborts.delete(conv.id)
      const hadOnlineAsk = conv.pendingPermission?.mode === 'online'
      // 非预期中断: 连接被掐断且未收到 done/error → 明确提示并给重试入口, 不再静默「卡住」
      if (streamInterrupted({ aborted, terminal, error: reply.error })) {
        reply.interrupted = true
        reply.error = hadOnlineAsk
          ? '连接中断，反问未送达；本轮已中止，可重试'
          : '连接中断，回答未完成；可重试'
        conv.error = reply.error
        console.warn('[chat] 在线流非预期中断', {
          sessionId: conv.id,
          aborted,
          terminal,
          hadOnlineAsk
        })
      }
      // 正常完成/出错/中止后, 残留的在线反问弹窗一并收起(避免回答后界面卡住)
      clearOnlinePendingPermission(conv)
      // 流式结束的通知只在正常完成时触发(中止/出错/空内容不打扰)
      if (shouldNotifyStreamFinish({ aborted, error: reply.error, content: reply.content })) {
        notifyStreamFinish(conv.id, reply.content)
      }
    }
  }

  /** 在线会话: 重试被中断的最后一轮(删除末尾助手占位后, 重发最后一条用户消息) */
  async function retryOnlineTurn(): Promise<void> {
    if (streaming.value || sessionMode.value === 'local') return
    if (sessionId.value && currentChannelKind.value && currentChannelKind.value !== 'web') return
    const conv = activeConversation()
    if (!conv) return
    let lastUser = -1
    for (let i = conv.messages.length - 1; i >= 0; i -= 1) {
      if (conv.messages[i].role === 'user') {
        lastUser = i
        break
      }
    }
    if (lastUser < 0) return
    const text = conv.messages[lastUser].content
    conv.messages.splice(lastUser)
    conv.error = ''
    await send(text)
  }

  /** 追加一轮用户消息与空的 assistant 回复,进入流式态 */
  function pushTurn(conv: Conversation, message: string): UiMessage {
    conv.messages.push({ ...blankMessage('user'), content: message })
    return pushReply(conv)
  }

  /** 只追加助手占位并进入流式态(重新生成/编辑重发复用: 用户消息已在列表里) */
  function pushReply(conv: Conversation): UiMessage {
    const reply = blankMessage('assistant')
    conv.messages.push(reply)
    conv.streaming = true
    conv.startedAt = Date.now()
    conv.error = ''
    return reply
  }

  /**
   * 本地流式回合公共段: invoke 拿 streamId → 订阅事件流(按 streamId 过滤) → 事件写回复气泡。
   * sendLocal/regenerate/editAndResend 复用; 停止与失败语义保持与原 sendLocal 一致。
   */
  async function runLocalTurn(invoke: () => Promise<{ streamId: string }>, conv: Conversation, reply: UiMessage): Promise<void> {
    try {
      const res = await invoke()
      // 等待 invoke 期间用户已点停止:finishLocalStream 已置 streaming=false,补发 stop 并放弃订阅
      if (!conv.streaming) {
        void window.desktop.invoke('localagent:stop', res.streamId).catch(() => {})
        return
      }
      const unsub = window.desktop.onLocalAgentEvent((event) => {
        // permission_request 可能不带 streamId(主进程找不到活跃流时),其余事件严格按 streamId 过滤;
        // 多会话并跑时再按 sessionId 兜底, 避免把别的会话的权限请求弹到当前会话
        if (event.type === 'permission_request') {
          if (event.sessionId && event.sessionId !== conv.id) return
          if (event.streamId && event.streamId !== res.streamId) return
        } else if (event.streamId !== res.streamId) {
          return
        }
        applyLocalEvent(conv, reply, event)
      })
      localStreams.set(conv.id, { streamId: res.streamId, unsub })
    } catch (err) {
      conv.error = (err as Error).message
      reply.error = (err as Error).message
      conv.streaming = false
      conv.startedAt = 0
    }
  }

  /** 本地分支:invoke chat 拿 streamId,再订阅事件流按 streamId 过滤 */
  async function sendLocal(message: string): Promise<void> {
    const conv = activeConversation()
    if (!conv) return
    const reply = pushTurn(conv, message)
    await runLocalTurn(
      () =>
        window.desktop.invoke<{ streamId: string }>('localagent:chat', {
          sessionId: conv.id,
          message
        }),
      conv,
      reply
    )
  }

  /**
   * 重新生成(C2, 仅本地): 删除 UI 里最后一条用户消息之后的消息,
   * 主进程同步截断存储并复用该用户消息重跑(不重复追加); 在线会话无此能力。
   */
  async function regenerate(): Promise<void> {
    if (streaming.value || !sessionId.value) return
    if (sessionMode.value !== 'local') return
    const conv = activeConversation()
    if (!conv) return
    let lastUser = -1
    for (let i = conv.messages.length - 1; i >= 0; i--) {
      if (conv.messages[i].role === 'user') {
        lastUser = i
        break
      }
    }
    if (lastUser < 0) {
      conv.error = '没有可重新生成的用户消息'
      return
    }
    conv.messages.splice(lastUser + 1)
    const reply = pushReply(conv)
    await runLocalTurn(
      () => window.desktop.invoke<{ streamId: string }>('localagent:regenerate', { sessionId: conv.id }),
      conv,
      reply
    )
  }

  /**
   * 编辑并重发(C3, 仅本地): 替换用户消息文本并删除其后消息, 主进程同步截断后重跑。
   * index 传「第几条用户消息」, 与存储层定位一致(存储里还夹着 assistant/tool 消息)。
   */
  async function editAndResend(message: UiMessage, text: string): Promise<void> {
    if (streaming.value || !sessionId.value) return
    if (sessionMode.value !== 'local' || message.role !== 'user') return
    const next = text.trim()
    if (!next) return
    const conv = activeConversation()
    if (!conv) return
    const at = conv.messages.indexOf(message)
    if (at < 0) return
    let userIndex = -1
    for (let i = 0; i <= at; i++) {
      if (conv.messages[i].role === 'user') userIndex += 1
    }
    if (userIndex < 0) return
    conv.messages.splice(at + 1)
    message.content = next
    const reply = pushReply(conv)
    await runLocalTurn(
      () =>
        window.desktop.invoke<{ streamId: string }>('localagent:editAndResend', {
          sessionId: conv.id,
          index: userIndex,
          text: next
        }),
      conv,
      reply
    )
  }

  /** 本地事件流分流(与主进程 AgentEvent 契约对齐) */
  function applyLocalEvent(conv: Conversation, target: UiMessage, event: LocalAgentEventPayload): void {
    switch (event.type) {
      case 'token':
        target.content += event.text ?? ''
        break
      case 'reasoning':
        target.reasoning += event.text ?? ''
        break
      case 'tool_call':
        target.tools.push({ name: event.name || 'tool', kind: 'tool', ts: Date.now() })
        break
      case 'tool_result': {
        const name = event.name || 'tool'
        const hit = [...target.tools]
          .reverse()
          .find((t) => t.name === name && t.kind === 'tool' && t.result === undefined)
        if (hit) hit.result = (event.ok === false ? '[执行失败] ' : '') + truncateText(event.output ?? '', 4000)
        break
      }
      case 'subagent_start':
        target.subagentRunning = true
        target.tools.push({ name: `子代理 ${event.name || ''}`.trim(), kind: 'subagent', ts: Date.now() })
        break
      case 'subagent_token':
        // 团队专家成员执行期间的流式输出: 实时累积展示
        target.subagentOutput += event.text ?? ''
        break
      case 'subagent_tool_call':
        target.tools.push({ name: `子代理 ${event.name || ''} · ${event.tool || 'tool'}`, kind: 'subagent', ts: Date.now() })
        break
      case 'subagent_tool_result': {
        const name = `子代理 ${event.name || ''} · ${event.tool || 'tool'}`
        const hit = [...target.tools]
          .reverse()
          .find((t) => t.name === name && t.kind === 'subagent' && t.result === undefined)
        if (hit) hit.result = (event.ok === false ? '[执行失败] ' : '') + truncateText(event.output ?? '', 4000)
        break
      }
      case 'subagent_end': {
        const name = `子代理 ${event.name || ''}`.trim()
        const hit = [...target.tools]
          .reverse()
          .find((t) => t.name === name && t.kind === 'subagent' && t.result === undefined)
        if (hit) hit.result = truncateText(event.output ?? '', 4000)
        target.subagentRunning = false
        break
      }
      case 'permission_request':
        conv.pendingPermission = {
          mode: 'local',
          requestId: event.requestId ?? '',
          tool: event.tool ?? '',
          summary: event.summary ?? ''
        }
        break
      case 'error':
        target.error = event.message || '未知错误'
        conv.error = target.error
        finishLocalStream(conv)
        break
      case 'done':
        finishLocalStream(conv)
        if (shouldNotifyStreamFinish({ aborted: false, error: target.error, content: target.content })) {
          notifyStreamFinish(conv.id, target.content)
        }
        break
      default:
        // 未知事件类型先忽略,保持前向兼容
        break
    }
  }

  /** 流已结束(done/error/abort): 收起残留的在线反问弹窗, 避免界面卡在等待回答 */
  function clearOnlinePendingPermission(conv: Conversation): void {
    if (conv.pendingPermission?.mode === 'online') conv.pendingPermission = null
  }

  /** 回答权限确认(local 工具权限 / online agent ask):先收起弹窗再回传 */
  async function respondPermission(
    decision: 'allow' | 'deny' | 'allow_session' | 'allow_always' | string
  ): Promise<void> {
    const conv = activeConversation()
    if (!conv) return
    const req = conv.pendingPermission
    if (!req) return
    conv.pendingPermission = null
    if (req.mode === 'online') {
      try {
        await agentApi.answerAsk(req.requestId, String(decision))
      } catch (err) {
        // 提交失败: 恢复待答状态以便重试, 并给出明确提示
        conv.pendingPermission = req
        throw new Error(`提交回答失败: ${(err as Error).message}`)
      }
      return
    }
    await window.desktop.invoke('localagent:permissions-respond', {
      requestId: req.requestId,
      decision: decision as 'allow' | 'deny' | 'allow_session' | 'allow_always'
    })
  }

  /** 在线事件写入指定会话(后台会话也能被正确更新) */
  function applyEventTo(conv: Conversation, target: UiMessage, event: ChatStreamEvent): void {
    switch (event.type) {
      case 'token':
        target.content += event.content ?? ''
        break
      case 'reasoning':
        target.reasoning += event.content ?? ''
        break
      case 'tool_start':
      case 'subagent_tool_start':
        target.tools.push({
          name: traceLabel(event.data ?? {}, event.type.startsWith('subagent') ? 'subagent' : 'tool'),
          kind: event.type.startsWith('subagent') ? 'subagent' : 'tool',
          ts: Date.now()
        })
        break
      case 'tool_result':
      case 'subagent_tool_result': {
        const kind = event.type.startsWith('subagent') ? 'subagent' : 'tool'
        const name = traceLabel(event.data ?? {}, kind)
        const hit = [...target.tools].reverse().find((t) => t.name === name && t.kind === kind && t.result === undefined)
        if (hit) hit.result = resultText(event.data?.result)
        break
      }
      case 'round_start':
      case 'subagent_round_start':
        // 轮次边界,UI 暂不展示
        break
      case 'subagent_start':
        target.subagentRunning = true
        target.tools.push({
          name: traceLabel(event.data ?? {}, 'subagent'),
          kind: 'subagent',
          ts: Date.now()
        })
        break
      case 'subagent_chat_event':
      case 'subagent_token': {
        // 子代理执行期间的流式输出: 实时累积并展示, 避免"结束才一次性出结果"
        const d = (event.data ?? {}) as { content?: string }
        target.subagentOutput += event.content ?? d.content ?? ''
        break
      }
      case 'ask': {
        // agent 反问(权限确认/ask_user):弹窗等待用户选择后 POST /api/chat/answer
        const d = (event.data ?? {}) as { ask_id?: string; question?: string; options?: string[] }
        conv.pendingPermission = {
          mode: 'online',
          requestId: event.ask_id ?? d.ask_id ?? '',
          tool: '',
          summary: event.question ?? d.question ?? '',
          options: event.options ?? d.options ?? ['允许', '拒绝']
        }
        break
      }
      case 'subagent_result': {
        // 回填到同名的进行中记录(与 tool_result 一致):原先每次 result 都新增一条,
        // 导致 subagent_start 那条永远没有结果,界面一直显示「执行中」。
        const name = traceLabel(event.data ?? {}, 'subagent')
        const result = resultText(event.data?.result ?? event.data?.content)
        const hit = [...target.tools]
          .reverse()
          .find((t) => t.name === name && t.kind === 'subagent' && t.result === undefined)
        if (hit) hit.result = result
        else
          target.tools.push({
            name,
            kind: 'subagent',
            result,
            ts: Date.now()
          })
        target.subagentRunning = false
        break
      }
      case 'done':
        // 服务端的全量结果权威性高于增量拼接
        if (event.content) target.content = event.content
        target.subagentRunning = false
        // 本轮已结束: 残留的在线反问弹窗一并收起
        clearOnlinePendingPermission(conv)
        break
      case 'error':
        target.error = event.content ?? '未知错误'
        target.subagentRunning = false
        conv.error = target.error
        clearOnlinePendingPermission(conv)
        break
      case 'heartbeat':
        break
      default:
        // 未知事件类型先忽略,保持前向兼容
        break
    }
  }

  /** 对外: 写入「当前会话」(测试/兼容 API) */
  function applyEvent(target: UiMessage, event: ChatStreamEvent): void {
    applyEventTo(ensureActive(), target, event)
  }

  return {
    // state
    agentUid,
    conversations,
    activeId,
    // 当前会话可写视图
    sessionId,
    sessionMode,
    currentChannelKind,
    localWorkspace,
    localPersona,
    localEphemeral,
    messages,
    streaming,
    pendingPermission,
    error,
    activeStartedAt,
    // actions
    newSession,
    resolveAgentUid,
    loadHistory,
    startLocalSession,
    switchMode,
    loadLocalMessages,
    isRunning,
    focus,
    dropConversation,
    finishLocalStream,
    stopStream,
    send,
    retryOnlineTurn,
    pushTurn,
    pushReply,
    runLocalTurn,
    sendLocal,
    regenerate,
    editAndResend,
    applyLocalEvent,
    clearOnlinePendingPermission,
    respondPermission,
    applyEvent,
    applyEventTo
  }
})

/** 在线历史按轮次归一化(每条 user 开启一轮, 其后 assistant/tool 合并) */
function normalizeHistory(history: HistoryMessage[]): UiMessage[] {
  const traceByCallId = new Map<string, ToolTrace>()
  const list: UiMessage[] = []
  let current: UiMessage | null = null
  for (const m of history) {
    if (m.role === 'user') {
      const user = blankMessage('user')
      user.content = m.content ?? ''
      list.push(user)
      current = null
      continue
    }
    if (m.role === 'tool') {
      const callId = m.tool_call_id ?? m.toolCallId ?? ''
      const trace = callId ? traceByCallId.get(callId) : undefined
      if (trace && trace.result === undefined) trace.result = truncateText(m.content ?? '', 4000)
      continue
    }
    // assistant:合并进当前轮;没有前置 user 时也单独成泡
    if (!current) {
      current = blankMessage('assistant')
      list.push(current)
    }
    if (m.content) current.content += current.content ? `\n\n${m.content}` : m.content
    if (m.reasoning_content) current.reasoning += m.reasoning_content
    for (const call of m.tool_calls ?? []) {
      const name = call.function?.name || call.name || 'tool'
      const trace: ToolTrace = { name, kind: 'tool', ts: Date.now() }
      current.tools.push(trace)
      if (call.id) traceByCallId.set(call.id, trace)
    }
  }
  return list
}

/**
 * 本地历史:assistant.toolCalls 映射为轨迹占位, tool 消息按 toolCallId 回填结果。
 *
 * 本地内核按「每段响应一条 assistant」落库(含大量单工具轮), 逐条成泡会出现
 * 大量"已调用 1 次工具"; 与在线历史同口径: 同一轮(相邻、无 user 隔断)的
 * assistant 合并为一个气泡 —— content 拼接、工具轨迹按出现顺序合并。
 */
export function normalizeLocalMessages(stored: LocalStoredMessage[]): UiMessage[] {
  const traceByCallId = new Map<string, ToolTrace>()
  const list: UiMessage[] = []
  let current: UiMessage | null = null
  for (const m of stored) {
    if (m.role === 'tool') {
      const trace = m.toolCallId ? traceByCallId.get(m.toolCallId) : undefined
      if (trace && trace.result === undefined) trace.result = truncateText(m.content, 4000)
      continue
    }
    if (m.role === 'user') {
      const user = blankMessage('user')
      user.content = m.content
      user.ts = m.ts ?? Date.now()
      list.push(user)
      current = null
      continue
    }
    if (!current) {
      current = blankMessage('assistant')
      current.ts = m.ts ?? Date.now()
      list.push(current)
    }
    if (m.content) current.content += current.content ? `\n\n${m.content}` : m.content
    for (const call of m.toolCalls ?? []) {
      const trace: ToolTrace = { name: call.name || 'tool', kind: 'tool', ts: m.ts ?? Date.now() }
      current.tools.push(trace)
      if (call.id) traceByCallId.set(call.id, trace)
    }
  }
  return list
}
