import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRegistry } from '../../electron/main/kernel/registry'

const demo = {
  name: 'demo', description: 'd', kind: 'read' as const,
  parameters: { type: 'object', properties: {} },
  execute: async () => ({ ok: true, output: 'ok' })
}

test('registry: register/get/list roundtrip', () => {
  const r = createRegistry()
  r.register(demo)
  assert.equal(r.get('demo')?.name, 'demo')
  assert.deepEqual(r.list().map(t => t.name), ['demo'])
})

test('registry: toOpenAiTools shapes function schema', () => {
  const r = createRegistry()
  r.register(demo)
  assert.deepEqual(r.toOpenAiTools(), [{
    type: 'function',
    function: { name: 'demo', description: 'd', parameters: { type: 'object', properties: {} } }
  }])
})

test('registry: duplicate register throws', () => {
  const r = createRegistry()
  r.register(demo)
  assert.throws(() => r.register(demo))
})

test('registry: unregister removes and reports presence', () => {
  const r = createRegistry()
  r.register(demo)
  assert.equal(r.unregister('missing'), false)
  assert.equal(r.unregister('demo'), true)
  assert.equal(r.get('demo'), undefined)
  assert.equal(r.list().length, 0)
  // 摘除后可重新注册同名工具（重装/失效重建场景）
  r.register(demo)
  assert.equal(r.get('demo')?.name, 'demo')
})
