import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'
import { useChatStore } from '../../src/stores/chat'
import { agentApi } from '../../src/api/agent'
import type { AgentUser, ChatStreamEvent } from '../../src/api/types'

/**
 * 渲染层 store 测试(tsconfig.web 下检查,带 DOM 类型;tsconfig.node 排除本目录)。
 * 启动期竞态回归:快速提问在 resolveAgentUid 的 await 窗口内,
 * autoOpenLatestSession/loadHistory 可能写入旧会话;forceNewSession 必须
 * 清空旧消息并换用新会话 ID,绝不把消息追加进旧会话。
 */
test('chat.send(forceNewSession)：并发 loadHistory 不会让消息进错会话', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  const captured: Array<{ session_id?: string }> = []

  agentApi.me = async (): Promise<AgentUser> => {
    // 模拟 await 窗口内自动打开最近会话(写入旧 sessionId 与旧消息)
    chat.loadHistory('web:42:old', [{ role: 'user', content: '旧消息' }])
    return { id: 42, name: 'tester', role: 'user' }
  }
  agentApi.streamChat = async (
    params: { session_id?: string },
    onEvent: (event: ChatStreamEvent) => void
  ): Promise<{ aborted: boolean }> => {
    captured.push(params)
    onEvent({ type: 'done', content: '' })
    return { aborted: false }
  }

  chat.sessionId = 'web:42:old'
  chat.sessionMode = 'agent'
  chat.messages = []
  await chat.send('新问题', { forceNewSession: true })

  assert.equal(captured.length, 1)
  assert.notEqual(captured[0].session_id, 'web:42:old')
  assert.ok(String(captured[0].session_id).startsWith('web:42:'))
  assert.equal(chat.sessionId, captured[0].session_id)
  // 只有本轮一问一答,旧会话消息已被清空
  assert.equal(chat.messages.length, 2)
  assert.equal(chat.messages[0].content, '新问题')
  assert.equal(chat.messages[1].role, 'assistant')
})

test('chat.send()：不强制新建时沿用当前会话', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  const captured: Array<{ session_id?: string }> = []

  agentApi.streamChat = async (
    params: { session_id?: string },
    onEvent: (event: ChatStreamEvent) => void
  ): Promise<{ aborted: boolean }> => {
    captured.push(params)
    onEvent({ type: 'done', content: '' })
    return { aborted: false }
  }

  chat.sessionId = 'web:42:keep'
  chat.sessionMode = 'agent'
  chat.messages = []
  await chat.send('继续追问')

  assert.equal(captured[0].session_id, 'web:42:keep')
  assert.equal(chat.sessionId, 'web:42:keep')
})
