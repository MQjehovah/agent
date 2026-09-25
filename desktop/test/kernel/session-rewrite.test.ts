import { test } from 'node:test'
import assert from 'node:assert/strict'
import { appendFileSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  createSessionStore,
  findNthUserMessageIndex,
  lastUserMessageIndex,
  removeEphemeralSessions
} from '../../electron/main/kernel/session'

/**
 * 消息级操作(C)与会话组织(D)的存储层纯逻辑:
 *   - rewriteMessages / truncateMessages: 原子重写 JSONL(临时文件 + rename), 刷新 updatedAt
 *   - 用户消息定位: 第 N 条(编辑重发) / 最后一条(重新生成)
 *   - ephemeral: 临时会话落盘标记与启动/退出清理
 */

const dataDir = mkdtempSync(join(tmpdir(), 'srw-'))
const store = createSessionStore(dataDir)
const sessionsDir = join(dataDir, 'localagent', 'sessions')

test('rewrite: 原子重写全量消息并刷新 updatedAt, 不残留临时文件', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  store.appendMessage(s.id, { role: 'user', content: '一' })
  store.appendMessage(s.id, { role: 'assistant', content: '二' })
  store.appendMessage(s.id, { role: 'user', content: '三' })

  // 截断到前两条并改写第二条(编辑重发的等价操作)
  const before = store.getMessages(s.id)
  const kept = store.rewriteMessages(s.id, [
    { role: 'user', content: '一' },
    { role: 'assistant', content: '二改', ts: before[1].ts }
  ])
  assert.equal(kept.length, 2)
  assert.deepEqual(store.getMessages(s.id).map((m) => m.content), ['一', '二改'])
  // 显式 ts 保留, 缺省 ts 补当前时间
  assert.equal(store.getMessages(s.id)[1].ts, before[1].ts)
  assert.ok(kept[0].ts > 0)

  // 文件是完整 JSONL: 每行一个对象, 末尾有换行, 无临时文件残留
  const file = join(sessionsDir, `${s.id}.messages.jsonl`)
  const lines = readFileSync(file, 'utf-8').split('\n')
  assert.equal(lines[lines.length - 1], '')
  assert.equal(lines.filter(Boolean).length, 2)
  assert.ok(!readdirSync(sessionsDir).some((name) => name.includes('.tmp')))

  // updatedAt 刷新(不早于创建时间)
  assert.ok(store.getSession(s.id)!.updatedAt >= s.updatedAt)
})

test('truncate: keepCount 0 清空消息且会话仍可继续追加', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  store.appendMessage(s.id, { role: 'user', content: 'a' })
  store.appendMessage(s.id, { role: 'assistant', content: 'b' })
  assert.deepEqual(store.truncateMessages(s.id, 0), [])
  assert.deepEqual(store.getMessages(s.id), [])
  store.appendMessage(s.id, { role: 'user', content: 'c' })
  assert.deepEqual(store.getMessages(s.id).map((m) => m.content), ['c'])
})

test('truncate: 超长按全量保留(幂等), 负数按 0 处理', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  store.appendMessage(s.id, { role: 'user', content: 'a' })
  store.appendMessage(s.id, { role: 'assistant', content: 'b' })
  assert.deepEqual(store.truncateMessages(s.id, 99).map((m) => m.content), ['a', 'b'])
  assert.deepEqual(store.truncateMessages(s.id, -3), [])
})

test('truncate: 重写时跳过并清除损坏行', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  store.appendMessage(s.id, { role: 'user', content: 'a' })
  const file = join(sessionsDir, `${s.id}.messages.jsonl`)
  // 模拟崩溃残留的损坏行
  appendFileSync(file, '{"role":"user","content":"broken\n')
  store.appendMessage(s.id, { role: 'assistant', content: 'b' })
  const kept = store.truncateMessages(s.id, 9)
  assert.deepEqual(kept.map((m) => m.content), ['a', 'b'])
  assert.ok(!readFileSync(file, 'utf-8').includes('broken'))
  // 重写后文件可正常解析
  assert.deepEqual(store.getMessages(s.id).map((m) => m.content), ['a', 'b'])
})

test('用户消息定位: 跳过 assistant/tool, 越界返回 -1', () => {
  const msgs = [
    { role: 'user' },
    { role: 'assistant' },
    { role: 'tool' },
    { role: 'user' },
    { role: 'assistant' }
  ]
  assert.equal(findNthUserMessageIndex(msgs, 0), 0)
  assert.equal(findNthUserMessageIndex(msgs, 1), 3)
  assert.equal(findNthUserMessageIndex(msgs, 2), -1)
  assert.equal(findNthUserMessageIndex(msgs, -1), -1)
  assert.equal(lastUserMessageIndex(msgs), 3)
  assert.equal(lastUserMessageIndex([{ role: 'assistant' }, { role: 'tool' }]), -1)
  assert.equal(lastUserMessageIndex([]), -1)
})

test('ephemeral: 落盘标记可读回, 启动/退出清理只删临时会话且幂等', () => {
  const keep = store.createSession({ model: 'm', workspace: 'ws' })
  const temp = store.createSession({ model: 'm', workspace: 'ws', ephemeral: true })
  assert.equal(store.getSession(temp.id)?.ephemeral, true)
  assert.equal(store.getSession(keep.id)?.ephemeral, undefined)
  // 列表里也能读到(本次运行内仍可回看)
  assert.ok(store.listSessions().some((s) => s.id === temp.id && s.ephemeral === true))

  const removed = removeEphemeralSessions(store)
  assert.deepEqual(removed, [temp.id])
  assert.equal(store.getSession(temp.id), null)
  assert.equal(store.getMessages(temp.id).length, 0)
  assert.ok(store.getSession(keep.id))
  // 再次清理幂等
  assert.deepEqual(removeEphemeralSessions(store), [])
})

test('rewrite/truncate: 非法 id 抛错, 未知会话不创建元数据', () => {
  assert.throws(() => store.rewriteMessages('../../evil', [{ role: 'user', content: 'x' }]), /非法会话 id/)
  assert.throws(() => store.truncateMessages('../../evil', 1), /非法会话 id/)
  store.rewriteMessages('local-00000000', [{ role: 'user', content: 'x' }])
  assert.equal(store.getSession('local-00000000'), null)
})

test('session-rewrite: teardown removes temp dir', () => {
  rmSync(dataDir, { recursive: true, force: true })
})
