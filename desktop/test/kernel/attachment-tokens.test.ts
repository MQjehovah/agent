import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  consumePickedPaths,
  PICK_TOKEN_TTL_MS,
  prunePickedPaths,
  resolvePickedPaths,
  storePickedPaths,
  type PickedPathsStore
} from '../../electron/main/kernel/attachment-tokens'

test('attachment-tokens: 存入后可按 token 取回源路径', () => {
  const store: PickedPathsStore = new Map()
  storePickedPaths(store, 'tok-1', ['C:\\a\\x.txt', 'C:\\b\\y.md'], 1_000)
  assert.deepEqual(resolvePickedPaths(store, 'tok-1', 1_000), ['C:\\a\\x.txt', 'C:\\b\\y.md'])
})

test('attachment-tokens: 未知 token 抛中文错误', () => {
  const store: PickedPathsStore = new Map()
  assert.throws(() => resolvePickedPaths(store, 'nope'), /请重新选择文件/)
})

test('attachment-tokens: 过期 token 抛错并被清理', () => {
  const store: PickedPathsStore = new Map()
  storePickedPaths(store, 'tok-exp', ['C:\\a\\x.txt'], 1_000, 5_000)
  assert.throws(() => resolvePickedPaths(store, 'tok-exp', 1_000 + 5_000), /请重新选择文件/)
  assert.equal(store.has('tok-exp'), false)
})

test('attachment-tokens: prune 只清过期条目', () => {
  const store: PickedPathsStore = new Map()
  storePickedPaths(store, 'old', ['a'], 0, 100)
  storePickedPaths(store, 'fresh', ['b'], 0, 10_000)
  prunePickedPaths(store, 500)
  assert.equal(store.has('old'), false)
  assert.equal(store.has('fresh'), true)
})

test('attachment-tokens: 消费后 token 不可二次使用', () => {
  const store: PickedPathsStore = new Map()
  storePickedPaths(store, 'once', ['C:\\a\\x.txt'], 1_000)
  assert.deepEqual(resolvePickedPaths(store, 'once', 1_000), ['C:\\a\\x.txt'])
  consumePickedPaths(store, 'once')
  assert.throws(() => resolvePickedPaths(store, 'once', 1_000), /请重新选择文件/)
})

test('attachment-tokens: 默认 TTL 为 10 分钟', () => {
  assert.equal(PICK_TOKEN_TTL_MS, 10 * 60 * 1000)
  const store: PickedPathsStore = new Map()
  storePickedPaths(store, 'ttl', ['a'], 0)
  assert.equal(store.get('ttl')?.expiresAt, PICK_TOKEN_TTL_MS)
})
