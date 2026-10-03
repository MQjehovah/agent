import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'
import { useChatStore } from '../../src/stores/chat'
import { useSettingsStore } from '../../src/stores/settings'
import { agentApi } from '../../src/api/agent'
import type { AgentUser, ChatStreamEvent } from '../../src/api/types'

/**
 * 多会话并跑: 一个会话运行中切到另一个会话, 原会话继续在后台更新气泡,
 * 随时可切回查看最新状态。
 */

const tick = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0))

/** 等直到条件满足(或超时), 用于等待 send 内部异步注册流 */
async function waitFor(check: () => boolean, tries = 20): Promise<void> {
  for (let i = 0; i < tries; i += 1) {
    if (check()) return
    await tick()
  }
}

function stubDesktopBridge(): void {
  Object.assign(globalThis, {
    window: {
      desktop: {
        invoke: async () => null,
        onUpstreamEvent: () => () => {},
        onLocalAgentEvent: () => () => {}
      }
    }
  })
}

test('在线: 会话A运行中切到B, A 后台继续更新, 切回可见最新', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  stubDesktopBridge()

  agentApi.me = async (): Promise<AgentUser> => ({ id: 42, name: 'tester', role: 'user' })
  const streams = new Map<string, (event: ChatStreamEvent) => void>()
  const resolvers = new Map<string, (outcome: { aborted: boolean }) => void>()
  agentApi.streamChat = (
    params: { session_id?: string },
    onEvent: (event: ChatStreamEvent) => void
  ): Promise<{ aborted: boolean }> => {
    const id = String(params.session_id)
    streams.set(id, onEvent)
    return new Promise((resolve) => resolvers.set(id, resolve))
  }

  const taskA = chat.send('问题A')
  await waitFor(() => streams.size > 0)
  const aId = [...streams.keys()][0]
  assert.ok(aId.startsWith('web:42:'))
  assert.equal(chat.sessionId, aId)
  assert.equal(chat.streaming, true)
  assert.equal(chat.messages.at(-1)?.role, 'assistant')

  // 切到另一个会话 B: A 转入后台, 不打断
  chat.loadHistory('web:42:bbbb', [])
  assert.equal(chat.sessionId, 'web:42:bbbb')
  assert.equal(chat.streaming, false)
  assert.equal(chat.isRunning(aId), true)
  assert.equal(chat.conversations[aId]?.streaming, true)

  // A 后台推 token: 即使不在前台也写入 A 的气泡
  streams.get(aId)!({ type: 'token', content: 'A流式' })
  assert.equal(chat.conversations[aId]?.messages.at(-1)?.content, 'A流式')
  assert.equal(chat.messages.length, 0) // B 仍为空, 未被 A 污染

  // 切回 A: 直接看到最新增量且仍为运行中
  assert.equal(chat.focus(aId), true)
  assert.equal(chat.sessionId, aId)
  assert.equal(chat.streaming, true)
  assert.equal(chat.messages.at(-1)?.content, 'A流式')

  // A 完成
  streams.get(aId)!({ type: 'done', content: 'A完成' })
  resolvers.get(aId)!({ aborted: false })
  await taskA
  assert.equal(chat.isRunning(aId), false)
  assert.equal(chat.streaming, false)
  assert.equal(chat.messages.at(-1)?.content, 'A完成')
  assert.equal(chat.conversations[aId]?.error, '')
})

test('在线: 用 loadHistory 覆盖后台在跑会话时不会清空其实时气泡', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  stubDesktopBridge()

  agentApi.me = async (): Promise<AgentUser> => ({ id: 7, name: 'tester', role: 'user' })
  const streams = new Map<string, (event: ChatStreamEvent) => void>()
  const resolvers = new Map<string, (outcome: { aborted: boolean }) => void>()
  agentApi.streamChat = (
    params: { session_id?: string },
    onEvent: (event: ChatStreamEvent) => void
  ): Promise<{ aborted: boolean }> => {
    const id = String(params.session_id)
    streams.set(id, onEvent)
    return new Promise((resolve) => resolvers.set(id, resolve))
  }

  const task = chat.send('hi')
  await waitFor(() => streams.size > 0)
  const id = [...streams.keys()][0]
  streams.get(id)!({ type: 'token', content: '增量' })

  // 未知调用方拉到旧历史后 loadHistory: 因该会话在跑, 只切换不覆盖
  chat.loadHistory(id, [{ role: 'user', content: '服务端旧记录' }])
  assert.equal(chat.sessionId, id)
  assert.equal(chat.messages.at(-1)?.content, '增量')

  streams.get(id)!({ type: 'done', content: '' })
  resolvers.get(id)!({ aborted: false })
  await task
})

test('本地: 两个本地会话并跑, 各自更新并可随时切换', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  const localListeners = new Set<(e: LocalAgentEventPayload) => void>()
  let created = 0
  Object.assign(globalThis, {
    window: {
      desktop: {
        invoke: async (channel: string, payload?: unknown) => {
          if (channel === 'localagent:sessions:create') {
            created += 1
            return {
              id: `local-${created}`,
              mode: 'local',
              title: 't',
              model: 'm',
              workspace: (payload as { workspace?: string })?.workspace ?? 'C:/ws',
              createdAt: 1,
              updatedAt: 1
            }
          }
          if (channel === 'localagent:chat') {
            return { streamId: `L-${(payload as { sessionId: string }).sessionId}` }
          }
          if (channel === 'localagent:messages') return []
          return null
        },
        onLocalAgentEvent: (cb: (e: LocalAgentEventPayload) => void) => {
          localListeners.add(cb)
          return () => {
            localListeners.delete(cb)
          }
        }
      }
    }
  })
  const emit = (e: LocalAgentEventPayload): void => {
    for (const cb of [...localListeners]) cb(e)
  }
  useSettingsStore().config.defaultWorkspace = 'C:/ws'

  await chat.startLocalSession()
  const aId = chat.sessionId
  const taskA = chat.send('A')
  await waitFor(() => localListeners.size > 0)
  emit({ type: 'token', text: 'A-流', streamId: `L-${aId}` })
  await tick()
  assert.equal(chat.messages.at(-1)?.content, 'A-流')

  await chat.startLocalSession()
  const bId = chat.sessionId
  assert.notEqual(aId, bId)
  assert.equal(chat.streaming, false) // 前台 B 尚未开跑
  assert.equal(chat.isRunning(aId), true) // A 仍在后台跑

  const taskB = chat.send('B')
  await waitFor(() => localListeners.size > 1)
  emit({ type: 'token', text: 'B-流', streamId: `L-${bId}` })
  await tick()
  assert.equal(chat.messages.at(-1)?.content, 'B-流')
  assert.equal(chat.conversations[aId]?.messages.at(-1)?.content, 'A-流')

  // 切回 A: 看到 A 的最新增量且仍在跑
  chat.focus(aId)
  assert.equal(chat.messages.at(-1)?.content, 'A-流')
  assert.equal(chat.streaming, true)

  // 两边分别结束
  emit({ type: 'done', streamId: `L-${aId}` })
  emit({ type: 'done', streamId: `L-${bId}` })
  await taskA
  await taskB
  assert.equal(chat.isRunning(aId), false)
  assert.equal(chat.isRunning(bId), false)
})

test('dropConversation: 删除会话会中止其后台流并从当前视图清出', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  stubDesktopBridge()

  agentApi.me = async (): Promise<AgentUser> => ({ id: 9, name: 'tester', role: 'user' })
  const streams = new Map<string, (event: ChatStreamEvent) => void>()
  let aborted = false
  agentApi.streamChat = (
    params: { session_id?: string },
    onEvent: (event: ChatStreamEvent) => void,
    signal?: AbortSignal
  ): Promise<{ aborted: boolean }> => {
    const id = String(params.session_id)
    streams.set(id, onEvent)
    return new Promise((resolve) => {
      signal?.addEventListener('abort', () => {
        aborted = true
        resolve({ aborted: true })
      })
    })
  }

  const task = chat.send('hi')
  await waitFor(() => streams.size > 0)
  const id = [...streams.keys()][0]
  assert.equal(chat.sessionId, id)

  chat.dropConversation(id)
  await task
  assert.equal(aborted, true)
  assert.equal(chat.sessionId, '')
  assert.equal(chat.conversations[id], undefined)
})
