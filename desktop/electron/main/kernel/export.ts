import type { LocalSession, StoredMessage } from './session'

/**
 * 会话导出(D2)纯逻辑: 把本地/在线会话归一为 ExportSession,
 * 组装 Markdown(按轮次, 含工具调用摘要)与 JSON, 并做导出文件名消毒。
 * 不 import electron: 保存对话框与写文件在 kernel/ipc.ts 里完成, 便于离线单测。
 */

export interface ExportToolCall {
  name: string
  arguments?: string
}

/** 归一后的导出消息(本地 camelCase 与在线 snake_case 都映射到这一形状) */
export interface ExportMessage {
  role: string
  content: string
  toolCalls?: ExportToolCall[]
  toolCallId?: string
  name?: string
  /** 在线历史的深度思考内容 */
  reasoning?: string
  ts?: number
}

export interface ExportSession {
  title: string
  mode: 'local' | 'agent'
  createdAt: number
  updatedAt: number
  messages: ExportMessage[]
}

/** 在线历史消息(agent /api/agent/sessions/messages 的宽松形状) */
export interface AgentHistoryMessage {
  role?: string
  content?: string
  tool_calls?: Array<{ id?: string; name?: string; function?: { name?: string; arguments?: string } }>
  tool_call_id?: string
  toolCallId?: string
  name?: string
  reasoning_content?: string
}

export interface AgentSessionInfo {
  id: string
  title?: unknown
  first_accessed?: unknown
  last_accessed?: unknown
  created_at?: unknown
}

const TOOL_SUMMARY_MAX = 200

/** 时间戳(数字/ISO 字符串)转 ms; 无法解析返回 0 */
export function toMillis(t: unknown): number {
  if (typeof t === 'number' && Number.isFinite(t)) return t
  if (typeof t === 'string') {
    const parsed = Date.parse(t)
    return Number.isNaN(parsed) ? 0 : parsed
  }
  return 0
}

/** 导出文件名消毒: 去 Windows 非法字符/控制字符, 压缩空白, 去首尾点, 截断 60 字; 空回退「会话」 */
export function sanitizeExportFileName(title: string): string {
  const cleaned = String(title ?? '')
    .replace(/[\\/:*?"<>|\u0000-\u001f\u007f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^[. ]+|[. ]+$/g, '')
  return cleaned.slice(0, 60).trim() || '会话'
}

/** 单行摘要: 空白折叠 + 超长截断 */
function oneLine(text: string, max = TOOL_SUMMARY_MAX): string {
  const t = String(text ?? '')
    .replace(/\s+/g, ' ')
    .trim()
  return t.length > max ? `${t.slice(0, max)}…` : t
}

/** 导出用时间文案(UTC, 便于跨机器一致); 0/非法值显示 '-' */
function formatTime(ts: number): string {
  if (!ts) return '-'
  return `${new Date(ts).toISOString().replace('T', ' ').slice(0, 19)} UTC`
}

/** 本地会话归一: StoredMessage(含 ts) → ExportMessage */
export function localToExportSession(meta: LocalSession, messages: StoredMessage[]): ExportSession {
  return {
    title: meta.title,
    mode: 'local',
    createdAt: meta.createdAt,
    updatedAt: meta.updatedAt,
    messages: messages.map((m) => {
      const out: ExportMessage = { role: m.role, content: m.content ?? '', ts: m.ts }
      if (m.toolCalls?.length) {
        out.toolCalls = m.toolCalls.map((c) => ({ name: c.name, arguments: c.arguments }))
      }
      if (m.toolCallId) out.toolCallId = m.toolCallId
      if (m.name) out.name = m.name
      return out
    })
  }
}

/** 在线会话归一: 会话信息 + HistoryMessage[] → ExportSession(标题缺失回退「会话 <id>」) */
export function agentToExportSession(info: AgentSessionInfo, history: AgentHistoryMessage[]): ExportSession {
  const title = String(info.title ?? '').trim() || `会话 ${info.id}`
  const createdAt = toMillis(info.first_accessed ?? info.created_at)
  const updatedAt = toMillis(info.last_accessed ?? info.first_accessed ?? info.created_at)
  const messages: ExportMessage[] = history.map((m) => {
    const out: ExportMessage = { role: String(m.role ?? ''), content: String(m.content ?? '') }
    const calls = m.tool_calls ?? []
    if (calls.length) {
      out.toolCalls = calls.map((c) => {
        const name = String(c.function?.name ?? c.name ?? 'tool')
        return c.function?.arguments ? { name, arguments: c.function.arguments } : { name }
      })
    }
    const callId = typeof m.tool_call_id === 'string' ? m.tool_call_id : m.toolCallId
    if (typeof callId === 'string' && callId) out.toolCallId = callId
    if (typeof m.name === 'string' && m.name) out.name = m.name
    if (typeof m.reasoning_content === 'string') out.reasoning = m.reasoning_content
    return out
  })
  return { title, mode: 'agent', createdAt, updatedAt, messages }
}

/**
 * Markdown 组装: 标题/时间头 + 按消息顺序的「## 用户 / ## 助手」轮次,
 * 助手发起工具调用时跟「### 工具调用」清单(arguments 单行摘要), 工具结果追加为「- 结果」。
 */
export function buildExportMarkdown(session: ExportSession): string {
  const out: string[] = []
  out.push(`# ${session.title || '会话'}`)
  out.push('')
  out.push(`- 模式：${session.mode === 'local' ? '本地' : '在线'}`)
  out.push(`- 创建时间：${formatTime(session.createdAt)}`)
  out.push(`- 更新时间：${formatTime(session.updatedAt)}`)

  let toolsOpen = false
  const openTools = (): void => {
    if (toolsOpen) return
    out.push('', '### 工具调用', '')
    toolsOpen = true
  }

  for (const m of session.messages) {
    if (m.role === 'user') {
      out.push('', '## 用户', '', m.content.trim() || '（空）')
      toolsOpen = false
      continue
    }
    if (m.role === 'assistant') {
      out.push('', '## 专家', '', m.content.trim() || '（无正文）')
      toolsOpen = false
      if (m.toolCalls?.length) {
        openTools()
        for (const call of m.toolCalls) {
          out.push(`- \`${call.name}\`${call.arguments ? `：${oneLine(call.arguments)}` : ''}`)
        }
      }
      continue
    }
    if (m.role === 'tool') {
      openTools()
      out.push(`- 结果${m.name ? `（${m.name}）` : ''}：${oneLine(m.content)}`)
      continue
    }
    out.push('', `## ${m.role}`, '', m.content.trim())
    toolsOpen = false
  }
  return `${out.join('\n')}\n`
}

/** JSON 导出: { title, mode, createdAt, updatedAt, messages } */
export function buildExportJson(session: ExportSession): string {
  return `${JSON.stringify(
    {
      title: session.title,
      mode: session.mode,
      createdAt: session.createdAt,
      updatedAt: session.updatedAt,
      messages: session.messages
    },
    null,
    2
  )}\n`
}
