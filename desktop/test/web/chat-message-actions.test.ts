import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'
import { useChatStore, type UiMessage } from '../../src/stores/chat'
import { useSettingsStore } from '../../src/stores/settings'

/**
 * 消息级操作(C)渲染层动作:重新生成 / 编辑重发(仅本地会话)。
 * 主进程存储截断由 kernel 单测覆盖;这里验证 IPC 契约、UI 截断顺序与流式事件复用。
 */

/** 安装最小 window.desktop 桩:记录 invoke,暴露本地事件回调 */
function stubDesktop(): {
  invokes: Array<{ channel: string; payload: unknown }>
  emit: (event: LocalAgentEventPayload) => void
} {
  const invokes: Array<{ channel: string; payload: unknown }> = []
  let listener: ((event: LocalAgentEventPayload) => void) | null = null
  Object.assign(globalThis, {
    window: {
      desktop: {
        invoke: async (channel: string, payload?: unknown) => {
          invokes.push({ channel, payload })
          if (channel === 'localagent:stop') return null
          return { streamId: 'stream-1' }
        },
        onLocalAgentEvent: (cb: (event: LocalAgentEventPayload) => void) => {
          listener = cb
          return () => {
            listener = null
          }
        }
      }
    }
  })
  return { invokes, emit: (event) => listener?.(event) }
}

function msg(role: 'user' | 'assistant', content: string): UiMessage {
  return {
    id: `${role}-${content}`,
    role,
    content,
    reasoning: '',
    subagentOutput: '',
    subagentRunning: false,
    tools: [],
    error: '',
    ts: 0
  }
}

const tick = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0))

test('chat.regenerate：截断到最后一条用户消息之后并复用流式事件', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  const { invokes, emit } = stubDesktop()
  chat.sessionId = 'local-00000001'
  chat.sessionMode = 'local'
  chat.messages = [
    msg('user', '旧问题'),
    msg('assistant', '旧回答'),
    msg('user', '新问题'),
    msg('assistant', '半截回答')
  ]

  const task = chat.regenerate()
  await tick()
  emit({ type: 'token', text: '重答', streamId: 'stream-1' })
  emit({ type: 'done', streamId: 'stream-1' })
  await task

  assert.deepEqual(invokes[0], { channel: 'localagent:regenerate', payload: { sessionId: 'local-00000001' } })
  // 完成后流式结束通知会额外 invoke desktop:notify:finish; 本地回合只发这一次
  assert.equal(invokes.filter((i) => String(i.channel).startsWith('localagent:')).length, 1)
  // 最后一条用户消息之后的助手消息被删除,只补一个新的助手占位
  assert.deepEqual(chat.messages.map((m) => m.role), ['user', 'assistant', 'user', 'assistant'])
  assert.deepEqual(chat.messages.map((m) => m.content), ['旧问题', '旧回答', '新问题', '重答'])
  assert.equal(chat.streaming, false)
})

test('chat.editAndResend：按用户消息序号定位,替换文本并截断其后', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  const { invokes, emit } = stubDesktop()
  chat.sessionId = 'local-00000002'
  chat.sessionMode = 'local'
  chat.messages = [
    msg('user', '第一问'),
    msg('assistant', '第一答'),
    msg('user', '第二问'),
    msg('assistant', '第二答')
  ]

  const task = chat.editAndResend(chat.messages[2], '第二问(改)')
  await tick()
  emit({ type: 'token', text: '新答', streamId: 'stream-1' })
  emit({ type: 'done', streamId: 'stream-1' })
  await task

  assert.equal(invokes[0].channel, 'localagent:editAndResend')
  // index 是「第几条用户消息」而不是消息数组下标(存储层还有 tool 消息)
  assert.deepEqual(invokes[0].payload, { sessionId: 'local-00000002', index: 1, text: '第二问(改)' })
  assert.deepEqual(chat.messages.map((m) => m.content), ['第一问', '第一答', '第二问(改)', '新答'])
  assert.equal(chat.streaming, false)
})

test('chat: 在线会话不提供重新生成/编辑重发(能力边界)', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  const { invokes } = stubDesktop()
  chat.sessionId = 'web:42:abcd'
  chat.sessionMode = 'agent'
  chat.messages = [msg('user', '问'), msg('assistant', '答')]

  await chat.regenerate()
  await chat.editAndResend(chat.messages[0], '改')

  assert.equal(invokes.length, 0)
  assert.deepEqual(chat.messages.map((m) => m.content), ['问', '答'])
})

test('chat: streaming 期间 regenerate/editAndResend 直接忽略', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  const { invokes } = stubDesktop()
  chat.sessionId = 'local-00000003'
  chat.sessionMode = 'local'
  chat.messages = [msg('user', '问'), msg('assistant', '答')]
  chat.streaming = true

  await chat.regenerate()
  await chat.editAndResend(chat.messages[0], '改')

  assert.equal(invokes.length, 0)
  assert.deepEqual(chat.messages.map((m) => m.content), ['问', '答'])
})

test('chat.editAndResend：调用失败时错误落到助手气泡并退出流式态', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  Object.assign(globalThis, {
    window: {
      desktop: {
        invoke: async () => {
          throw new Error('消息状态异常,请重试')
        },
        onLocalAgentEvent: () => () => {}
      }
    }
  })
  chat.sessionId = 'local-00000004'
  chat.sessionMode = 'local'
  chat.messages = [msg('user', '问'), msg('assistant', '答')]

  await chat.editAndResend(chat.messages[0], '改')

  assert.equal(chat.error, '消息状态异常,请重试')
  assert.equal(chat.messages[chat.messages.length - 1].error, '消息状态异常,请重试')
  assert.equal(chat.streaming, false)
})

test('chat.startLocalSession(ephemeral)：临时标记上报并置位,普通会话清除', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  const invokes: Array<{ channel: string; payload: Record<string, unknown> }> = []
  Object.assign(globalThis, {
    window: {
      desktop: {
        invoke: async (channel: string, payload?: unknown) => {
          invokes.push({ channel, payload: (payload ?? {}) as Record<string, unknown> })
          if (channel === 'localagent:sessions:create') {
            return {
              id: 'local-00000005',
              mode: 'local',
              title: 't',
              model: 'm',
              workspace: 'C:/ws',
              createdAt: 1,
              updatedAt: 1
            }
          }
          if (channel === 'localagent:messages') return []
          return null
        },
        onLocalAgentEvent: () => () => {}
      }
    }
  })
  useSettingsStore().config.defaultWorkspace = 'C:/ws'

  await chat.startLocalSession(undefined, undefined, { ephemeral: true })
  assert.equal(chat.localEphemeral, true)
  assert.equal(invokes.find((i) => i.channel === 'localagent:sessions:create')?.payload.ephemeral, true)

  // 切回普通会话清除临时标记
  await chat.startLocalSession()
  assert.equal(chat.localEphemeral, false)

  // 历史加载按 meta 透传临时标记
  await chat.loadLocalMessages('local-00000005', { workspace: 'C:/ws', ephemeral: true })
  assert.equal(chat.localEphemeral, true)
  chat.newSession()
  assert.equal(chat.localEphemeral, false)
})
