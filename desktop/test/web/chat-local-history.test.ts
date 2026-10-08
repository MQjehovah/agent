import { test } from 'node:test'
import assert from 'node:assert/strict'
import { normalizeLocalMessages } from '../../src/stores/chat'
import type { LocalStoredMessage } from '../../src/api/types'

/**
 * 本地会话历史回放: 本地内核按「每段响应一条 assistant」落库, 回放需与在线历史
 * 同口径按轮合并, 避免大量"已调用 1 次工具"小气泡刷屏。
 */

test('本地历史: 相邻 assistant(单工具轮)合并为一泡, 工具轨迹按序合并/结果回填', () => {
  const stored: LocalStoredMessage[] = [
    { role: 'user', content: '帮我构建', ts: 1 },
    { role: 'assistant', content: '开始', toolCalls: [{ id: 'c1', name: 'terminal', arguments: '{}' }], ts: 2 },
    { role: 'tool', toolCallId: 'c1', content: 'out1', ts: 3 },
    { role: 'assistant', content: '', toolCalls: [{ id: 'c2', name: 'git', arguments: '{}' }], ts: 4 },
    { role: 'tool', toolCallId: 'c2', content: 'out2', ts: 5 },
    { role: 'assistant', content: '完成', ts: 6 },
    { role: 'user', content: '再来', ts: 7 },
    { role: 'assistant', content: '好的', ts: 8 }
  ]

  const list = normalizeLocalMessages(stored)
  assert.equal(list.length, 4)
  assert.equal(list[0].role, 'user')
  assert.equal(list[1].role, 'assistant')
  assert.equal(list[1].tools.length, 2)
  assert.deepEqual(
    list[1].tools.map((t) => t.name),
    ['terminal', 'git']
  )
  assert.equal(list[1].tools[0].result, 'out1')
  assert.equal(list[1].tools[1].result, 'out2')
  assert.equal(list[1].content, '开始\n\n完成')
  assert.equal(list[1].ts, 2)
  assert.equal(list[2].role, 'user')
  assert.equal(list[2].content, '再来')
  assert.equal(list[3].role, 'assistant')
  assert.equal(list[3].content, '好的')
})

test('本地历史: 首条即 assistant 时单独成泡; tool 结果无占位时忽略', () => {
  const stored: LocalStoredMessage[] = [
    { role: 'assistant', content: 'x', ts: 1 },
    { role: 'tool', toolCallId: 'ghost', content: 'orphan', ts: 2 },
    { role: 'assistant', content: 'y', ts: 3 }
  ]
  const list = normalizeLocalMessages(stored)
  assert.equal(list.length, 1)
  assert.equal(list[0].role, 'assistant')
  assert.equal(list[0].content, 'x\n\ny')
  assert.equal(list[0].tools.length, 0)
})
