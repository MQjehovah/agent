import { contextBridge, ipcRenderer, type IpcRendererEvent } from 'electron'

/** 渲染进程允许调用的主进程通道白名单 */
const ALLOWED_CHANNELS = new Set([
  'config:get',
  'config:set',
  'app:info',
  'sso:start',
  'auth:me',
  'auth:logout',
  'usage:get',
  'upstream:request',
  'upstream:stream:start',
  'upstream:stream:abort',
  'localagent:sessions:list',
  'localagent:sessions:create',
  'localagent:sessions:delete',
  'localagent:sessions:rename',
  'localagent:sessions:setModel',
  'localagent:models:list',
  'localagent:messages',
  'localagent:context:usage',
  'localagent:workspace:pick',
  'localagent:workspace:list',
  'localagent:workspace:create',
  'localagent:file:pick',
  'localagent:file:paste',
  'localagent:attach:import',
  'agent:attach:upload',
  'artifact:list',
  'artifact:read',
  'artifact:reveal',
  'artifact:open',
  'localagent:chat',
  'localagent:stop',
  'localagent:regenerate',
  'localagent:editAndResend',
  'sessions:archive:list',
  'sessions:archive:set',
  'sessions:export',
  'localagent:permissions-respond',
  'localagent:permissions:list-remembered',
  'localagent:permissions:forget',
  'localagent:permissions:clear-remembered',
  'localagent:skills:list',
  'localagent:market:listMy',
  'localagent:market:subscribe',
  'localagent:market:unsubscribe',
  'localagent:market:install',
  'localagent:market:uninstall',
  'localagent:market:listInstalled',
  'localagent:personas:list',
  'localagent:mcp:status',
  'localagent:mcp:set-enabled',
  'localagent:mcp:env:get',
  'localagent:mcp:env:set',
  'localagent:permission:mode',
  'quick:submit',
  'quick:reject',
  'quick:close',
  'desktop:ready',
  'app:loginitem:get',
  'app:loginitem:set',
  'app:hotkey:status',
  'desktop:notify:finish',
  'desktop:screenshot',
  'desktop:asr:transcribe',
  'desktop:asr:abort',
  'app:update:check',
  'app:update:install',
  'app:update:status'
])

/** 上游事件(SSE 流帧/结束/错误),按 streamId 区分 */
export type UpstreamEvent = {
  streamId: string
  type: 'status' | 'chunk' | 'end' | 'error'
  status?: number
  text?: string
  message?: string
  aborted?: boolean
}

/** 本地 agent 事件流载荷:与主进程 AgentEvent 对齐并附带 streamId */
export type LocalAgentEventPayload = {
  streamId: string
} & (
  | { type: 'token'; text: string }
  | { type: 'tool_call'; name: string; args: string }
  | { type: 'tool_result'; name: string; output: string; ok: boolean }
  | { type: 'permission_request'; requestId: string; sessionId: string; tool: string; summary: string; streamId?: string }
  | { type: 'done' }
  | { type: 'error'; message: string }
)

/** localagent:file:pick 返回值：取消，或回传主进程签发的一次性 token（不再回传绝对路径） */
export type FilePickResult = { canceled: true } | { canceled: false; token: string }

/** localagent:attach:import 入参：只认主进程 token，渲染层无法指定源路径 */
export type AttachImportRequest = { sessionId: string; token: string }

/** localagent:attach:import 结果：成功导入的相对路径与跳过原因 */
export type AttachImportResult = {
  imported: { relPath: string; name: string }[]
  skipped: { path: string; reason: string }[]
}

/** 快速提问窗提交后,主进程转发给主窗口的文本 */
export type QuickPromptPayload = { text: string }

/** 快速提问受理失败/被拒时回传给快速窗的原因 */
export type QuickErrorPayload = { message: string }

/** 通知点击后,主进程要求主窗口打开的会话 */
export type OpenSessionPayload = { sessionId: string }

/** 语音转写(E)入参: 音频字节 + 可选文件名/MIME; requestId 供取消透传 */
export type AsrTranscribeRequest = {
  bytes: Uint8Array | ArrayBuffer
  name?: string
  mime?: string
  requestId?: string
}

/** 语音转写结果: 失败折叠为可读 error(未配置/超时/取消/HTTP/网络) */
export type AsrTranscribeResult = { ok: true; text: string } | { ok: false; error: string }

/** 本地会话上下文用量(与 buildContext 同口径) */
export type ContextUsageResult = {
  used: number
  budget: number
  messages: number
  total: number
  model: string
}

/** 自动更新状态快照(主进程 app:update:* 返回与 desktop:update-status 推送) */
export type UpdateStatusPayload = {
  event: 'unconfigured' | 'idle' | 'checking' | 'available' | 'not-available' | 'downloaded' | 'error'
  message: string
  version?: string
}

const desktop = {
  invoke: (channel: string, payload?: unknown): Promise<unknown> => {
    if (!ALLOWED_CHANNELS.has(channel)) {
      return Promise.reject(new Error(`Channel not allowed: ${channel}`))
    }
    return ipcRenderer.invoke(channel, payload)
  },

  /** 订阅上游流事件,返回取消订阅函数 */
  onUpstreamEvent: (callback: (event: UpstreamEvent) => void): (() => void) => {
    const listener = (_e: IpcRendererEvent, event: UpstreamEvent): void => callback(event)
    ipcRenderer.on('upstream:event', listener)
    return () => ipcRenderer.removeListener('upstream:event', listener)
  },

  /** 订阅本地 agent 事件流,返回取消订阅函数 */
  onLocalAgentEvent: (callback: (event: LocalAgentEventPayload) => void): (() => void) => {
    const listener = (_e: IpcRendererEvent, event: LocalAgentEventPayload): void => callback(event)
    ipcRenderer.on('localagent:event', listener)
    return () => ipcRenderer.removeListener('localagent:event', listener)
  },

  /** 订阅快速提问文本(主进程转发),返回取消订阅函数 */
  onQuickPrompt: (callback: (payload: QuickPromptPayload) => void): (() => void) => {
    const listener = (_e: IpcRendererEvent, payload: QuickPromptPayload): void => callback(payload)
    ipcRenderer.on('desktop:quick-prompt', listener)
    return () => ipcRenderer.removeListener('desktop:quick-prompt', listener)
  },

  /** 订阅快速提问错误回执(保留快速窗并回显原因),返回取消订阅函数 */
  onQuickError: (callback: (payload: QuickErrorPayload) => void): (() => void) => {
    const listener = (_e: IpcRendererEvent, payload: QuickErrorPayload): void => callback(payload)
    ipcRenderer.on('quick:error', listener)
    return () => ipcRenderer.removeListener('quick:error', listener)
  },

  /** 订阅「打开指定会话」(通知点击),返回取消订阅函数 */
  onOpenSession: (callback: (payload: OpenSessionPayload) => void): (() => void) => {
    const listener = (_e: IpcRendererEvent, payload: OpenSessionPayload): void => callback(payload)
    ipcRenderer.on('desktop:open-session', listener)
    return () => ipcRenderer.removeListener('desktop:open-session', listener)
  },

  /** 订阅「新建会话」(托盘菜单),返回取消订阅函数 */
  onNewSession: (callback: () => void): (() => void) => {
    const listener = (): void => callback()
    ipcRenderer.on('desktop:new-session', listener)
    return () => ipcRenderer.removeListener('desktop:new-session', listener)
  },

  /** 订阅自动更新状态变化(检查中/发现新版本/已下载/失败),返回取消订阅函数 */
  onUpdateStatus: (callback: (payload: UpdateStatusPayload) => void): (() => void) => {
    const listener = (_e: IpcRendererEvent, payload: UpdateStatusPayload): void => callback(payload)
    ipcRenderer.on('desktop:update-status', listener)
    return () => ipcRenderer.removeListener('desktop:update-status', listener)
  }
}

export type DesktopApi = typeof desktop

contextBridge.exposeInMainWorld('desktop', desktop)
