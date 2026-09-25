import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type { WebContents } from 'electron'
import { createSessionStore } from '../../electron/main/kernel/session'
import { createTurnRegistry, type TurnClaim } from '../../electron/main/kernel/turn-registry'
import {
  editAndResendLocalTurn,
  regenerateLocalTurn,
  startLocalTurn,
  type SessionActionDeps
} from '../../electron/main/kernel/session-actions'

/**
 * 轮次编排（评审 1）: **同步 claim(并发守卫)先于任何破坏性写**。
 * 守卫命中时截断/改写不发生（存储零改动），execute 不被调用；
 * execute 失败时释放 claim（不残留占用）。
 */

const dataDir = mkdtempSync(join(tmpdir(), 'sact-'))
const store = createSessionStore(dataDir)

function fakeSender(): WebContents {
  const sender = {
    once: () => sender,
    removeListener: () => sender,
    isDestroyed: () => false
  }
  return sender as unknown as WebContents
}

interface Harness {
  reg: ReturnType<typeof createTurnRegistry>
  deps: SessionActionDeps<TurnClaim>
  executed: Array<{ userMessage: string; history: Array<{ role: string; content: string }> }>
}

function harness(overrides: Partial<SessionActionDeps<TurnClaim>> = {}): Harness {
  const reg = createTurnRegistry()
  const executed: Harness['executed'] = []
  const deps: SessionActionDeps<TurnClaim> = {
    store,
    claim: (sessionId) => reg.claim(sessionId, fakeSender()),
    execute: async (_claim, _session, userMessage, options) => {
      executed.push({ userMessage, history: options.history })
      return { streamId: 's-ok' }
    },
    maxContextChars: 100_000,
    ...overrides
  }
  return { reg, deps, executed }
}

function seedSession(): string {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  store.appendMessage(s.id, { role: 'user', content: '问1' })
  store.appendMessage(s.id, { role: 'assistant', content: '答1' })
  store.appendMessage(s.id, { role: 'user', content: '问2' })
  store.appendMessage(s.id, { role: 'assistant', content: '答2' })
  return s.id
}

test('session-actions: regenerate 守卫失败时存储零改动, execute 不被调用', async () => {
  const { reg, deps, executed } = harness()
  const id = seedSession()
  // 模拟该会话已有活跃流(另一轮生成中)
  const busy = reg.claim(id, fakeSender())

  await assert.rejects(() => regenerateLocalTurn(deps, id), /该会话正在对话中/)

  assert.equal(executed.length, 0)
  assert.deepEqual(store.getMessages(id).map((m) => m.content), ['问1', '答1', '问2', '答2'])

  // 释放后正常: 截断到最后一条用户消息并复用其内容重跑
  busy.release()
  await regenerateLocalTurn(deps, id)
  assert.deepEqual(store.getMessages(id).map((m) => m.content), ['问1', '答1', '问2'])
  assert.equal(executed.length, 1)
  assert.equal(executed[0].userMessage, '问2')
  // 历史不含目标用户消息(由 userMessage 单独传给 loop)
  assert.deepEqual(executed[0].history.map((m) => m.content), ['问1', '答1'])
})

test('session-actions: editAndResend 守卫失败时消息未被替换/截断', async () => {
  const { reg, deps, executed } = harness()
  const id = seedSession()
  const busy = reg.claim(id, fakeSender())

  await assert.rejects(() => editAndResendLocalTurn(deps, id, 0, '改过的问题'), /该会话正在对话中/)

  assert.equal(executed.length, 0)
  assert.deepEqual(store.getMessages(id).map((m) => m.content), ['问1', '答1', '问2', '答2'])

  busy.release()
  await editAndResendLocalTurn(deps, id, 0, '改过的问题')
  // 替换第 0 条并删除其后全部消息
  assert.deepEqual(store.getMessages(id).map((m) => m.content), ['改过的问题'])
  assert.equal(executed[0].userMessage, '改过的问题')
  assert.deepEqual(executed[0].history, [])
})

test('session-actions: claim 先于破坏性写; execute 失败释放 claim 无残留', async () => {
  const events: string[] = []
  const reg = createTurnRegistry()
  const id = seedSession()
  const deps: SessionActionDeps<TurnClaim> = {
    store,
    claim: (sessionId) => {
      events.push('claim')
      return reg.claim(sessionId, fakeSender())
    },
    execute: async () => {
      events.push('execute')
      throw new Error('请先完成企业 SSO 登录')
    },
    maxContextChars: 100_000
  }

  await assert.rejects(() => regenerateLocalTurn(deps, id), /SSO 登录/)

  // 顺序: 先 sync claim(守卫) → 再异步 execute; execute 抛错后 release
  assert.deepEqual(events, ['claim', 'execute'])
  assert.equal(reg.hasActive(id), false)
  assert.equal(reg.senders.has(id), false)
  assert.equal(reg.streams.size, 0)
})

test('session-actions: chat 的 claim 先于 execute(守卫先于 loop 落盘用户消息)', async () => {
  const { reg, deps, executed } = harness()
  const id = seedSession()
  const before = store.getMessages(id).length
  const busy = reg.claim(id, fakeSender())

  await assert.rejects(() => startLocalTurn(deps, id, '新问题'), /该会话正在对话中/)

  assert.equal(executed.length, 0)
  // 守卫命中: 没有任何新用户消息落盘(loop 未启动)
  assert.equal(store.getMessages(id).length, before)

  busy.release()
  await startLocalTurn(deps, id, '新问题')
  assert.equal(executed.length, 1)
  assert.equal(executed[0].userMessage, '新问题')
  // 历史为该会话既有消息(buildContext 结果)
  assert.deepEqual(executed[0].history.map((m) => m.content), ['问1', '答1', '问2', '答2'])
})

test('session-actions: 非法入参在 claim 之前拦截(不产生占用)', async () => {
  const { reg, deps } = harness()
  const id = seedSession()
  await assert.rejects(() => startLocalTurn(deps, id, '   '), /消息不能为空/)
  await assert.rejects(() => editAndResendLocalTurn(deps, id, -1, 'x'), /消息序号无效/)
  await assert.rejects(() => editAndResendLocalTurn(deps, id, 99, 'x'), /找不到要编辑的用户消息/)
  assert.equal(reg.streams.size, 0)
})

test('session-actions: teardown removes temp dir', () => {
  rmSync(dataDir, { recursive: true, force: true })
})
