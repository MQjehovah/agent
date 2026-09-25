import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'
import { useChatStore, type PendingPermission, type UiMessage } from '../../src/stores/chat'

/**
 * 在线反问(ask_user)回答与流结束清理:
 * 回答提交失败要恢复待答状态可重试; 流结束/出错/中止要收起残留弹窗, 避免界面卡住。
 */

/** 安装最小 window.desktop 桩: 记录 invoke, 可按需让某通道失败 */
function stubDesktop(options: { failAnswer?: boolean } = {}): {
  invokes: Array<{ channel: string; payload: Record<string, unknown> }>
} {
  const invokes: Array<{ channel: string; payload: Record<string, unknown> }> = []
  Object.assign(globalThis, {
    window: {
      desktop: {
        invoke: async (channel: string, payload?: unknown) => {
          invokes.push({ channel, payload: (payload ?? {}) as Record<string, unknown> })
          if (channel === 'upstream:request') {
            const path = (payload as { path?: string } | undefined)?.path ?? ''
            if (options.failAnswer && path === '/api/chat/answer') {
              throw new Error('网络错误')
            }
            return { status: 200, text: '{}' }
          }
          return null
        }
      }
    }
  })
  return { invokes }
}

function onlineAsk(): PendingPermission {
  return { mode: 'online', requestId: 'ask-1', tool: '', summary: '是否继续?', options: ['继续', '停止'] }
}

function replyMessage(): UiMessage {
  return {
    id: 'a-1',
    role: 'assistant',
    content: '',
    reasoning: '',
    subagentOutput: '',
    subagentRunning: false,
    tools: [],
    error: '',
    ts: 0
  }
}

test('chat.respondPermission(online)：调用 answerAsk 并清空待答状态', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  const { invokes } = stubDesktop()
  chat.sessionMode = 'agent'
  chat.pendingPermission = onlineAsk()

  await chat.respondPermission('继续')

  assert.equal(chat.pendingPermission, null)
  const answer = invokes.find((i) => i.channel === 'upstream:request')
  assert.deepEqual(answer?.payload, {
    service: 'agent',
    path: '/api/chat/answer',
    method: 'POST',
    body: { ask_id: 'ask-1', answer: '继续' }
  })
})

test('chat.respondPermission(online)：提交失败恢复待答状态并抛出「提交回答失败」', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  stubDesktop({ failAnswer: true })
  chat.sessionMode = 'agent'
  const req = onlineAsk()
  chat.pendingPermission = req

  await assert.rejects(() => chat.respondPermission('允许'), /提交回答失败/)

  // 弹窗恢复, 用户可重试
  assert.deepEqual(chat.pendingPermission, req)
})

test('chat.applyEvent：流 done/error 清理残留的在线反问弹窗, 不影响 local', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  stubDesktop()
  const reply = replyMessage()

  chat.pendingPermission = onlineAsk()
  chat.applyEvent(reply, { type: 'done', content: '回答内容' })
  assert.equal(chat.pendingPermission, null)
  assert.equal(reply.content, '回答内容')

  chat.pendingPermission = onlineAsk()
  chat.applyEvent(reply, { type: 'error', content: '服务端错误' })
  assert.equal(chat.pendingPermission, null)
  assert.equal(chat.error, '服务端错误')

  // local 权限弹窗由本地流程管理, 在线事件清理不得误伤
  chat.pendingPermission = { mode: 'local', requestId: 'perm-1', tool: 'write_file', summary: '写入' }
  chat.applyEvent(reply, { type: 'done' })
  assert.equal(chat.pendingPermission?.mode, 'local')
})

test('chat.respondPermission(local)：仍走 localagent 通道且清空待答状态', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  const { invokes } = stubDesktop()
  chat.sessionMode = 'local'
  chat.pendingPermission = { mode: 'local', requestId: 'perm-1', tool: 'write_file', summary: '写入' }

  await chat.respondPermission('deny')

  assert.equal(chat.pendingPermission, null)
  assert.deepEqual(invokes[0], {
    channel: 'localagent:permissions-respond',
    payload: { requestId: 'perm-1', decision: 'deny' }
  })
})
