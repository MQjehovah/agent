import { test } from 'node:test'
import assert from 'node:assert/strict'
import type { WebContents } from 'electron'
import { createTurnRegistry } from '../../electron/main/kernel/turn-registry'

/**
 * 轮次占用登记（评审 1）: 同会话并发守卫 + streams/senders 账本。
 * claim 同步、失败零副作用；release 幂等且失败路径不残留；窗口销毁触发业务清理回调。
 */

/** 最小 WebContents 桩: once/removeListener/isDestroyed + 手动 destroy 触发 */
function fakeSender(): WebContents & { destroy(): void } {
  const listeners = new Map<string, Array<() => void>>()
  let destroyed = false
  const sender = {
    once(event: string, fn: () => void) {
      const list = listeners.get(event) ?? []
      list.push(fn)
      listeners.set(event, list)
      return sender
    },
    removeListener(event: string, fn: () => void) {
      listeners.set(event, (listeners.get(event) ?? []).filter((f) => f !== fn))
      return sender
    },
    isDestroyed: () => destroyed,
    destroy() {
      destroyed = true
      for (const fn of listeners.get('destroyed') ?? []) fn()
    }
  }
  return sender as unknown as WebContents & { destroy(): void }
}

function countingIds(): () => string {
  let n = 0
  return () => `s${++n}`
}

test('turn-registry: 同会话并发 claim 抛错且零副作用', () => {
  const reg = createTurnRegistry({ newStreamId: countingIds() })
  const first = reg.claim('local-00000001', fakeSender())
  assert.equal(reg.hasActive('local-00000001'), true)
  assert.equal(reg.streamIdOf('local-00000001'), first.streamId)
  assert.equal(reg.senderOf('local-00000001'), reg.senders.get('local-00000001'))

  const before = reg.streams.size
  assert.throws(() => reg.claim('local-00000001', fakeSender()), /该会话正在对话中/)
  // 守卫失败不新增流/不覆盖 sender
  assert.equal(reg.streams.size, before)
  assert.equal(reg.streams.get(first.streamId)?.sessionId, 'local-00000001')
})

test('turn-registry: 不同会话可并发; release 幂等、可重新 claim 且无残留', () => {
  const reg = createTurnRegistry({ newStreamId: countingIds() })
  const a = reg.claim('local-a', fakeSender())
  reg.claim('local-b', fakeSender())
  assert.equal(reg.streams.size, 2)

  a.release()
  assert.equal(a.controller.signal.aborted, true)
  assert.equal(reg.hasActive('local-a'), false)
  assert.equal(reg.senders.has('local-a'), false)
  assert.equal(reg.hasActive('local-b'), true)
  // 幂等: 重复 release 不影响其它会话
  a.release()
  assert.equal(reg.hasActive('local-b'), true)

  // 释放后可重新 claim(失败路径不残留占用)
  const again = reg.claim('local-a', fakeSender())
  assert.equal(reg.hasActive('local-a'), true)
  again.release()
  assert.equal(reg.senders.has('local-a'), false)
})

test('turn-registry: settle 删流并在无活跃流时清 sender + 回调 onIdle', () => {
  const reg = createTurnRegistry({ newStreamId: countingIds() })
  const claim = reg.claim('local-c', fakeSender())
  const idle: string[] = []
  reg.settle(claim.streamId, 'local-c', (sid) => idle.push(sid))
  assert.equal(reg.streams.size, 0)
  assert.equal(reg.senders.has('local-c'), false)
  assert.deepEqual(idle, ['local-c'])

  // 同会话多流(理论上不出现, 防御性): settle 其中一条不触发 onIdle
  const one = reg.claim('local-d', fakeSender())
  const extra = { controller: new AbortController(), sessionId: 'local-d' }
  reg.streams.set('extra', extra)
  reg.settle(one.streamId, 'local-d', (sid) => idle.push(sid))
  assert.equal(reg.senders.has('local-d'), true)
  assert.deepEqual(idle, ['local-c'])
  reg.streams.delete('extra')
})

test('turn-registry: 窗口销毁 abort 该会话流、清账本并回调业务清理', () => {
  const cleaned: string[] = []
  const reg = createTurnRegistry({
    onSenderDestroyed: (sessionId) => cleaned.push(sessionId),
    newStreamId: countingIds()
  })
  const sender = fakeSender()
  const claim = reg.claim('local-e', sender)
  sender.destroy()

  assert.equal(claim.controller.signal.aborted, true)
  assert.equal(reg.hasActive('local-e'), false)
  assert.equal(reg.senders.has('local-e'), false)
  assert.deepEqual(cleaned, ['local-e'])
  // release 幂等: 不再重复回调
  claim.release()
  assert.deepEqual(cleaned, ['local-e'])
})

test('turn-registry: senderOf 对已销毁 sender 返回 null 并清账本', () => {
  const reg = createTurnRegistry({ newStreamId: countingIds() })
  const sender = fakeSender()
  reg.claim('local-f', sender)
  sender.destroy()
  assert.equal(reg.senderOf('local-f'), null)
  assert.equal(reg.senders.has('local-f'), false)
})
