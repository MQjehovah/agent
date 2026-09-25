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
import { shouldNotifyStreamFinish } from '../utils/stream'

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

/** 进行中的流式请求控制器(不可序列化,不放进 store state) */
let activeAbort: AbortController | null = null

/** 本地模式进行中的流:streamId 用于 stop,unsub 用于结束/停止时摘除事件监听 */
let activeLocalStreamId: string | null = null
let activeLocalUnsub: (() => void) | null = null

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

export const useChatStore = defineStore('chat', {
  state: () => ({
    sessionId: '',
    /** agent 侧用户 uid(拼 web:{uid}:{rand} 会话 ID 用,登录后缓存) */
    agentUid: '',
    /** 当前会话模式:agent 零号员工 / local 本地 agent */
    sessionMode: 'agent' as 'agent' | 'local',
    /** 当前会话渠道细类: 空(新会话)/web/dingtalk/dingtalk_group/local; 非 web 的在线会话只读 */
    currentChannelKind: '' as string,
    /** 本地会话工作区路径(标题区 tooltip 展示) */
    localWorkspace: '',
    /** 本地会话当前人设(市场安装的 agent;空=默认) */
    localPersona: '',
    /** 当前本地会话是否为临时会话(退出后自动删除, 不落历史) */
    localEphemeral: false,
    messages: [] as UiMessage[],
    streaming: false,
    error: '',
    /** 待确认的本地工具权限请求(非空时弹窗) */
    pendingPermission: null as PendingPermission | null
  }),

  actions: {
    newSession() {
      if (this.streaming) return
      this.sessionId = ''
      this.sessionMode = 'agent'
      this.currentChannelKind = ''
      this.localWorkspace = ''
      this.localPersona = ''
      this.localEphemeral = false
      this.messages = []
      this.error = ''
      this.pendingPermission = null
    },

    /** 取 agent 侧 uid(优先缓存,失败则查 /api/auth/me);拿不到返回空串 */
    async resolveAgentUid(): Promise<string> {
      if (this.agentUid) return this.agentUid
      try {
        const me = await agentApi.me()
        const uid = me?.id === undefined || me?.id === null ? '' : String(me.id)
        if (uid) this.agentUid = uid
        return uid
      } catch {
        return ''
      }
    },

    /**
     * 加载在线会话历史:agent 历史是「一条消息一行」(含 role=tool 与多次 assistant),
     * 直接铺平会拆成一堆小气泡;这里按轮次归一化 —— 每条 user 开启一轮,
     * 其后的 assistant/tool 合并进同一个气泡(content 拼接、reasoning 累积、
     * tool_calls 建轨迹占位、tool 结果按 tool_call_id 回填)。
     */
    loadHistory(sessionId: string, messages: HistoryMessage[]) {
      if (this.streaming) return
      this.sessionId = sessionId
      this.sessionMode = 'agent'
      this.currentChannelKind = channelKindFromId(sessionId)
      this.localWorkspace = ''
      this.localEphemeral = false
      this.error = ''
      this.pendingPermission = null

      const blank = (role: 'user' | 'assistant'): UiMessage => ({
        id: nextId('m'),
        role,
        content: '',
        reasoning: '',
        subagentOutput: '',
        subagentRunning: false,
        tools: [],
        error: '',
        ts: Date.now()
      })

      const traceByCallId = new Map<string, ToolTrace>()
      const list: UiMessage[] = []
      let current: UiMessage | null = null

      for (const m of messages) {
        if (m.role === 'user') {
          const user = blank('user')
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
          current = blank('assistant')
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

      this.messages = list
    },

    /**
     * 新建本地会话:工作区优先级 = 显式指定 > 默认工作区 > 弹窗选择(并记住为默认);
     * 可指定市场安装的人设; options.ephemeral 创建临时会话(退出即删)。
     */
    async startLocalSession(
      personaName?: string,
      workspaceOverride?: string,
      options: { ephemeral?: boolean } = {}
    ) {
      if (this.streaming) return
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
      this.sessionId = meta.id
      this.sessionMode = 'local'
      this.currentChannelKind = 'local'
      this.localWorkspace = meta.workspace
      this.localPersona = meta.personaName ?? personaName ?? ''
      this.localEphemeral = Boolean(options.ephemeral)
      this.messages = []
      this.error = ''
      this.pendingPermission = null
    },

    /** 模式切换:切到本地模式会创建(或复用默认工作区)本地会话;切回零号员工回到 agent 空态 */
    async switchMode(mode: 'agent' | 'local') {
      if (this.streaming || mode === this.sessionMode) return
      if (mode === 'local') {
        await this.startLocalSession()
      } else {
        this.newSession()
      }
    },

    /** 加载本地会话历史:assistant.toolCalls 映射为轨迹占位,tool 消息按 toolCallId 回填结果 */
    async loadLocalMessages(sessionId: string, meta?: { workspace?: string; ephemeral?: boolean }) {
      if (this.streaming) return
      const stored = await window.desktop.invoke<LocalStoredMessage[]>('localagent:messages', sessionId)
      this.sessionId = sessionId
      this.sessionMode = 'local'
      this.currentChannelKind = 'local'
      this.localWorkspace = meta?.workspace ?? ''
      this.localEphemeral = meta?.ephemeral === true
      this.error = ''
      this.pendingPermission = null
      const traceByCallId = new Map<string, ToolTrace>()
      const list: UiMessage[] = []
      for (const m of stored) {
        if (m.role === 'tool') {
          const trace = m.toolCallId ? traceByCallId.get(m.toolCallId) : undefined
          if (trace && trace.result === undefined) trace.result = truncateText(m.content, 4000)
          continue
        }
        const ts = m.ts ?? Date.now()
        const ui: UiMessage = {
          id: nextId('m'),
          role: m.role === 'user' ? 'user' : 'assistant',
          content: m.content,
          reasoning: '',
          subagentOutput: '',
          subagentRunning: false,
          tools: [],
          error: '',
          ts
        }
        if (m.role === 'assistant' && m.toolCalls?.length) {
          for (const call of m.toolCalls) {
            const trace: ToolTrace = { name: call.name || 'tool', kind: 'tool', ts }
            ui.tools.push(trace)
            traceByCallId.set(call.id, trace)
          }
        }
        list.push(ui)
      }
      this.messages = list
    },

    /** 结束本地流:摘除监听、退出流式态并收起权限弹窗 */
    finishLocalStream() {
      if (activeLocalUnsub) {
        activeLocalUnsub()
        activeLocalUnsub = null
      }
      activeLocalStreamId = null
      this.streaming = false
      this.pendingPermission = null
    },

    stopStream() {
      if (this.sessionMode === 'local') {
        if (activeLocalStreamId) {
          void window.desktop.invoke('localagent:stop', activeLocalStreamId).catch(() => {})
        }
        this.finishLocalStream()
        return
      }
      activeAbort?.abort()
    },

    async send(text: string, options: { forceNewSession?: boolean } = {}) {
      const message = text.trim()
      if (!message || this.streaming) return
      if (this.sessionMode === 'local') {
        if (!this.sessionId) {
          this.error = '请先选择工作区创建本地会话'
          return
        }
        await this.sendLocal(message)
        return
      }

      // 非 web 在线会话(钉钉等)只读: 阻止写回(服务端会 400), 给出明确提示
      if (this.sessionId && this.currentChannelKind && this.currentChannelKind !== 'web') {
        this.error = '该会话来自钉钉等外部渠道，仅支持查看历史，请回到对应渠道继续对话'
        return
      }

      if (!this.sessionId || options.forceNewSession) {
        const uid = await this.resolveAgentUid()
        if (!uid) {
          this.error = '无法获取账号信息，请重新登录后再试'
          return
        }
        // 强制新建:等待 uid 期间可能被并发 loadHistory 写入旧会话,清空后换用新 ID
        if (options.forceNewSession) {
          this.messages = []
          this.error = ''
        }
        this.sessionId = newSessionId(uid)
      }
      const reply = this.pushTurn(message)
      const controller = new AbortController()
      activeAbort = controller
      const apply = (event: ChatStreamEvent) => this.applyEvent(reply, event)

      let aborted = false
      try {
        const settings = useSettingsStore()
        const outcome = await agentApi.streamChat(
          { message, session_id: this.sessionId, permission_mode: settings.permissionMode },
          apply,
          controller.signal
        )
        aborted = outcome.aborted
      } catch (err) {
        if ((err as Error).name !== 'AbortError') {
          this.error = (err as Error).message
          reply.error = (err as Error).message
        } else {
          aborted = true
        }
      } finally {
        this.streaming = false
        activeAbort = null
        // 正常完成/出错/中止后, 残留的在线反问弹窗一并收起(避免回答后界面卡住)
        this.clearOnlinePendingPermission()
        // 流式结束的通知只在正常完成时触发(中止/出错/空内容不打扰)
        if (shouldNotifyStreamFinish({ aborted, error: reply.error, content: reply.content })) {
          notifyStreamFinish(this.sessionId, reply.content)
        }
      }
    },

    /** 追加一轮用户消息与空的 assistant 回复,进入流式态 */
    pushTurn(message: string): UiMessage {
      this.messages.push({
        id: nextId('u'),
        role: 'user',
        content: message,
        reasoning: '',
        subagentOutput: '',
        subagentRunning: false,
        tools: [],
        error: '',
        ts: Date.now()
      })
      return this.pushReply()
    },

    /** 只追加助手占位并进入流式态(重新生成/编辑重发复用: 用户消息已在列表里) */
    pushReply(): UiMessage {
      const reply: UiMessage = {
        id: nextId('a'),
        role: 'assistant',
        content: '',
        reasoning: '',
        subagentOutput: '',
        subagentRunning: false,
        tools: [],
        error: '',
        ts: Date.now()
      }
      this.messages.push(reply)
      this.streaming = true
      this.error = ''
      return reply
    },

    /**
     * 本地流式回合公共段: invoke 拿 streamId → 订阅事件流(按 streamId 过滤) → 事件写回复气泡。
     * sendLocal/regenerate/editAndResend 复用; 停止与失败语义保持与原 sendLocal 一致。
     */
    async runLocalTurn(invoke: () => Promise<{ streamId: string }>, reply: UiMessage) {
      try {
        const res = await invoke()
        // 等待 invoke 期间用户已点停止:finishLocalStream 已置 streaming=false,补发 stop 并放弃订阅
        if (!this.streaming) {
          void window.desktop.invoke('localagent:stop', res.streamId).catch(() => {})
          return
        }
        activeLocalStreamId = res.streamId
        activeLocalUnsub = window.desktop.onLocalAgentEvent((event) => {
          // permission_request 可能不带 streamId(主进程找不到活跃流时),其余事件严格按 streamId 过滤
          if (event.type === 'permission_request') {
            if (event.streamId && event.streamId !== res.streamId) return
          } else if (event.streamId !== res.streamId) {
            return
          }
          this.applyLocalEvent(reply, event)
        })
      } catch (err) {
        this.error = (err as Error).message
        reply.error = (err as Error).message
        this.streaming = false
      }
    },

    /** 本地分支:invoke chat 拿 streamId,再订阅事件流按 streamId 过滤 */
    async sendLocal(message: string) {
      const reply = this.pushTurn(message)
      await this.runLocalTurn(
        () =>
          window.desktop.invoke<{ streamId: string }>('localagent:chat', {
            sessionId: this.sessionId,
            message
          }),
        reply
      )
    },

    /**
     * 重新生成(C2, 仅本地): 删除 UI 里最后一条用户消息之后的消息,
     * 主进程同步截断存储并复用该用户消息重跑(不重复追加); 在线会话无此能力。
     */
    async regenerate() {
      if (this.streaming || !this.sessionId) return
      if (this.sessionMode !== 'local') return
      let lastUser = -1
      for (let i = this.messages.length - 1; i >= 0; i--) {
        if (this.messages[i].role === 'user') {
          lastUser = i
          break
        }
      }
      if (lastUser < 0) {
        this.error = '没有可重新生成的用户消息'
        return
      }
      this.messages.splice(lastUser + 1)
      const reply = this.pushReply()
      await this.runLocalTurn(
        () => window.desktop.invoke<{ streamId: string }>('localagent:regenerate', { sessionId: this.sessionId }),
        reply
      )
    },

    /**
     * 编辑并重发(C3, 仅本地): 替换用户消息文本并删除其后消息, 主进程同步截断后重跑。
     * index 传「第几条用户消息」, 与存储层定位一致(存储里还夹着 assistant/tool 消息)。
     */
    async editAndResend(message: UiMessage, text: string) {
      if (this.streaming || !this.sessionId) return
      if (this.sessionMode !== 'local' || message.role !== 'user') return
      const next = text.trim()
      if (!next) return
      const at = this.messages.indexOf(message)
      if (at < 0) return
      let userIndex = -1
      for (let i = 0; i <= at; i++) {
        if (this.messages[i].role === 'user') userIndex += 1
      }
      if (userIndex < 0) return
      this.messages.splice(at + 1)
      message.content = next
      const reply = this.pushReply()
      await this.runLocalTurn(
        () =>
          window.desktop.invoke<{ streamId: string }>('localagent:editAndResend', {
            sessionId: this.sessionId,
            index: userIndex,
            text: next
          }),
        reply
      )
    },

    /** 本地事件流分流(与主进程 AgentEvent 契约对齐) */
    applyLocalEvent(target: UiMessage, event: LocalAgentEventPayload) {
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
        case 'permission_request':
          this.pendingPermission = {
            mode: 'local',
            requestId: event.requestId ?? '',
            tool: event.tool ?? '',
            summary: event.summary ?? ''
          }
          break
        case 'error':
          target.error = event.message || '未知错误'
          this.error = target.error
          this.finishLocalStream()
          break
        case 'done':
          this.finishLocalStream()
          if (shouldNotifyStreamFinish({ aborted: false, error: target.error, content: target.content })) {
            notifyStreamFinish(this.sessionId, target.content)
          }
          break
        default:
          // 未知事件类型先忽略,保持前向兼容
          break
      }
    },

    /** 流已结束(done/error/abort): 收起残留的在线反问弹窗, 避免界面卡在等待回答 */
    clearOnlinePendingPermission() {
      if (this.pendingPermission?.mode === 'online') this.pendingPermission = null
    },

    /** 回答权限确认(local 工具权限 / online agent ask):先收起弹窗再回传 */
    async respondPermission(decision: 'allow' | 'deny' | 'allow_session' | 'allow_always' | string) {
      const req = this.pendingPermission
      if (!req) return
      this.pendingPermission = null
      if (req.mode === 'online') {
        try {
          await agentApi.answerAsk(req.requestId, String(decision))
        } catch (err) {
          // 提交失败: 恢复待答状态以便重试, 并给出明确提示
          this.pendingPermission = req
          throw new Error(`提交回答失败: ${(err as Error).message}`)
        }
        return
      }
      await window.desktop.invoke('localagent:permissions-respond', {
        requestId: req.requestId,
        decision: decision as 'allow' | 'deny' | 'allow_session' | 'allow_always'
      })
    },

    applyEvent(target: UiMessage, event: ChatStreamEvent) {
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
          this.pendingPermission = {
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
          this.clearOnlinePendingPermission()
          break
        case 'error':
          target.error = event.content ?? '未知错误'
          target.subagentRunning = false
          this.error = target.error
          this.clearOnlinePendingPermission()
          break
        case 'heartbeat':
          break
        default:
          // 未知事件类型先忽略,保持前向兼容
          break
      }
    }
  }
})
