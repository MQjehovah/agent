import { request, streamRequest } from './client'
import type { AgentUser, ChatStreamEvent, HistoryMessage, LoginResponse, SessionInfo } from './types'

export const agentApi = {
  login(username: string, password: string): Promise<LoginResponse> {
    return request('agent', '/api/auth/login', { method: 'POST', body: { username, password } })
  },

  me(): Promise<AgentUser> {
    return request('agent', '/api/auth/me')
  },

  status(): Promise<Record<string, unknown>> {
    return request('agent', '/api/agent/status')
  },

  /** 在线会话列表:对话口径(跨渠道合并),字段见服务端 /api/agent/sessions/history */
  listSessions(limit = 50): Promise<{ sessions: SessionInfo[] }> {
    return request('agent', `/api/agent/sessions/history?limit=${limit}`)
  },

  /** 会话历史消息(内存会话优先, 未加载则回退数据库) */
  sessionMessages(sessionId: string): Promise<{ messages: HistoryMessage[] }> {
    return request('agent', `/api/agent/sessions/messages?session_id=${encodeURIComponent(sessionId)}`)
  },

  /** 重命名/置顶会话(在线) */
  updateSession(sessionId: string, patch: { title?: string; pinned?: boolean }): Promise<unknown> {
    return request('agent', `/api/agent/sessions?session_id=${encodeURIComponent(sessionId)}`, {
      method: 'PATCH',
      body: patch
    })
  },

  /** 在会话消息内容里检索 */
  searchSessions(q: string, limit = 30): Promise<{
    total: number
    results: Array<{ conversation_id: string; title?: string; snippet?: string; hits?: number; channel?: string }>
  }> {
    return request('agent', `/api/agent/sessions/search?q=${encodeURIComponent(q)}&limit=${limit}`)
  },

  /** 删除在线会话(对话根); 群会话/外部渠道会话由服务端拒绝 */
  deleteSession(sessionId: string): Promise<unknown> {
    return request('agent', `/api/agent/sessions?session_id=${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
  },

  /**
   * 流式对话,事件回调(协议见 agent/src/web/server.py):
   * token / reasoning / heartbeat / tool_* / subagent_* / ask / done / error
   */
  streamChat(
    params: {
      message: string
      session_id?: string
      /** 会话级权限模式(在线): default 每次询问 / smart 必要时询问 / auto 完全访问 */
      permission_mode?: 'default' | 'smart' | 'auto'
    },
    onEvent: (event: ChatStreamEvent) => void,
    signal?: AbortSignal
  ): Promise<{ aborted: boolean }> {
    return streamRequest<ChatStreamEvent>('agent', '/api/chat/stream', params, onEvent, signal)
  },

  /** 回答 agent 反问(权限确认 / ask_user) */
  answerAsk(askId: string, answer: string): Promise<unknown> {
    return request('agent', '/api/chat/answer', { method: 'POST', body: { ask_id: askId, answer } })
  }
}
