import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createArchiveStore, normalizeArchiveKey } from '../../electron/main/archive'

/**
 * 会话归档(D1)纯逻辑: archived.json 读写、去重、损坏容错、非法键拒绝。
 * 与 session store 同约定: 直接读写文件、原子写(临时文件 + rename)。
 */

const dir = mkdtempSync(join(tmpdir(), 'arch-'))
const file = join(dir, 'archived.json')
const store = createArchiveStore(file)

test('archive: set 增删查与去重', () => {
  assert.deepEqual(store.list(), [])
  store.set('local:local-abcd1234', true)
  store.set('agent:web:42:deadbeef', true)
  // 重复归档同一键不产生重复条目
  store.set('local:local-abcd1234', true)
  assert.deepEqual(store.list(), ['local:local-abcd1234', 'agent:web:42:deadbeef'])
  assert.equal(store.has('local:local-abcd1234'), true)
  assert.equal(store.has('agent:web:99:nope'), false)

  store.set('local:local-abcd1234', false)
  assert.deepEqual(store.list(), ['agent:web:42:deadbeef'])
  // 取消不存在的键是幂等 no-op
  assert.deepEqual(store.set('agent:web:99:nope', false), ['agent:web:42:deadbeef'])
})

test('archive: 落盘持久化, 跨实例可见, 重启后归档状态保持', () => {
  const again = createArchiveStore(file)
  assert.deepEqual(again.list(), ['agent:web:42:deadbeef'])
  again.set('local:local-00000001', true)
  const third = createArchiveStore(file)
  assert.ok(third.has('local:local-00000001'))
  assert.ok(Array.isArray(JSON.parse(readFileSync(file, 'utf-8'))))
})

test('archive: 损坏文件与非字符串/重复条目容错', () => {
  const bad = join(dir, 'bad.json')
  writeFileSync(bad, '{not json', 'utf-8')
  assert.deepEqual(createArchiveStore(bad).list(), [])
  writeFileSync(bad, JSON.stringify(['local:a', 'local:a', '', 42, null, 'agent:b']), 'utf-8')
  assert.deepEqual(createArchiveStore(bad).list(), ['local:a', 'agent:b'])
})

test('archive: 非法键拒绝且不落盘', () => {
  const isolated = createArchiveStore(join(dir, 'isolated.json'))
  assert.throws(() => isolated.set('', true), /归档键无效/)
  assert.throws(() => isolated.set('   ', true), /归档键无效/)
  assert.throws(() => isolated.set('localonly', true), /归档键无效/)
  assert.throws(() => isolated.set('local:\u0000x', true), /归档键无效/)
  assert.equal(isolated.has('localonly'), false)
  assert.deepEqual(isolated.list(), [])

  // normalize: 两端空白剥除, 非字符串/空串归一为空
  assert.equal(normalizeArchiveKey('  local:local-abcd1234  '), 'local:local-abcd1234')
  assert.equal(normalizeArchiveKey(42), '')
  assert.equal(normalizeArchiveKey(null), '')
})

test('archive: 原子写不残留临时文件', () => {
  const atomic = createArchiveStore(join(dir, 'atomic.json'))
  atomic.set('local:x', true)
  atomic.set('local:y', true)
  assert.deepEqual(atomic.list(), ['local:x', 'local:y'])
  assert.ok(!readdirSync(dir).some((name) => name.includes('.tmp')))
})

test('archive: teardown removes temp dir', () => {
  rmSync(dir, { recursive: true, force: true })
})
