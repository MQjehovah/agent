import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createMarketTool, createMarketToolInvoker } from '../../electron/main/kernel/market-tools'
import type { ToolContext } from '../../electron/main/kernel/types'

const ctx: ToolContext = { workspace: 'w', sessionId: 's' }

/** 记录 fetch 调用的最小字段 */
interface Call {
  input: string
  init: {
    method?: string
    headers?: Record<string, string>
    body?: string
  }
}

function stubFetch(calls: Call[], handler: () => Response): typeof fetch {
  return async (input, init) => {
    const req = init ?? {}
    calls.push({
      input: String(input),
      init: {
        method: req.method,
        headers: req.headers as Record<string, string> | undefined,
        body: req.body !== undefined ? String(req.body) : undefined
      }
    })
    return handler()
  }
}

test('market-tool: createMarketTool 生成 market:<name> 的 read 工具与单参 schema', async () => {
  const calls: Array<{ name: string; params: unknown }> = []
  const tool = createMarketTool({ name: 'doc-check', description: '检查文档规范' }, async (name, params) => {
    calls.push({ name, params })
    return 'OK'
  })
  assert.equal(tool.name, 'market:doc-check')
  assert.equal(tool.kind, 'read')
  assert.deepEqual(tool.parameters, {
    type: 'object',
    properties: { params: { type: 'object', description: '市场工具入参对象，具体键由该工具定义' } },
    required: ['params']
  })
  const res = await tool.execute({ params: { strict: true } }, ctx)
  assert.equal(res.ok, true)
  assert.equal(res.output, 'OK')
  assert.deepEqual(calls, [{ name: 'doc-check', params: { strict: true } }])
})

test('market-tool: execute 省略 params 时以空对象调用', async () => {
  const calls: Array<{ name: string; params: unknown }> = []
  const tool = createMarketTool({ name: 'noop' }, async (name, params) => {
    calls.push({ name, params })
    return null
  })
  const res = await tool.execute({}, ctx)
  assert.equal(res.ok, true)
  assert.equal(res.output, 'null')
  assert.deepEqual(calls, [{ name: 'noop', params: {} }])
})

test('market-tool: 对象返回值折叠为 JSON 文本', async () => {
  const tool = createMarketTool({ name: 'x' }, async () => ({ status: 'ok', output: 'hello' }))
  const res = await tool.execute({ params: {} }, ctx)
  assert.equal(res.ok, true)
  assert.deepEqual(JSON.parse(res.output), { status: 'ok', output: 'hello' })
})

test('market-tool: description 缺失时给缺省文案', async () => {
  const tool = createMarketTool({ name: 'ghost' }, async () => undefined)
  assert.equal(tool.description, '调用市场远程工具 ghost：调用时把该工具的入参整体放入 params 对象。')
  const res = await tool.execute({ params: {} }, ctx)
  assert.equal(res.ok, true)
  assert.equal(res.output, '(无输出)')
})

test('market-tool: invoke 抛异常折叠为 ok:false 且保留远程错误 detail', async () => {
  const tool = createMarketTool({ name: 'x', description: 'd' }, async () => {
    throw new Error('市场远程调用失败(HTTP 500)：服务端崩溃')
  })
  const res = await tool.execute({ params: {} }, ctx)
  assert.equal(res.ok, false)
  assert.ok(res.output.includes('HTTP 500'), res.output)
  assert.ok(res.output.includes('服务端崩溃'), res.output)
})

test('market-tool: 循环引用的返回值不抛异常（String 兜底）', async () => {
  const cyclic: Record<string, unknown> = {}
  cyclic.self = cyclic
  const tool = createMarketTool({ name: 'x' }, async () => cyclic)
  const res = await tool.execute({ params: {} }, ctx)
  assert.equal(res.ok, true)
})

test('market-tool invoker: 成功调用 POST /api/runtime/tools/<name>/invoke 并返回 result', async () => {
  const calls: Call[] = []
  const invoke = createMarketToolInvoker({
    marketUrl: 'http://market.test/',
    getToken: () => 'sso-tok',
    fetchImpl: stubFetch(calls, () =>
      new Response(JSON.stringify({
        capability: { name: 'doc-check', type: 'tool' },
        action: 'invoke',
        message: '工具调用成功',
        result: { tool: 'doc-check', version: '1.0.0', status: 'ok', output: '检查通过' }
      }), { status: 200 })
    )
  })
  const out = await invoke('doc-check', { path: 'a.txt' })
  assert.equal(calls.length, 1)
  assert.equal(calls[0].input, 'http://market.test/api/runtime/tools/doc-check/invoke')
  assert.equal(calls[0].init.method, 'POST')
  assert.equal(calls[0].init.headers?.authorization, 'Bearer sso-tok')
  assert.equal(calls[0].init.body, JSON.stringify({ params: { path: 'a.txt' } }))
  assert.equal((out as { status?: string }).status, 'ok')
  assert.equal((out as { output?: string }).output, '检查通过')
})

test('market-tool invoker: HTTP 非 2xx 抛中文错误并带 detail', async () => {
  const invoke = createMarketToolInvoker({
    marketUrl: 'http://market.test',
    getToken: () => 'sso-tok',
    fetchImpl: async () =>
      new Response(JSON.stringify({ detail: '能力不存在或不可见' }), { status: 404 })
  })
  await assert.rejects(invoke('ghost', {}), (err: unknown) => {
    const message = err instanceof Error ? err.message : String(err)
    assert.ok(message.includes('404'), message)
    assert.ok(message.includes('能力不存在或不可见'), message)
    assert.ok(/[\u4e00-\u9fff]/.test(message), `错误文案应为中文: ${message}`)
    return true
  })
})

test('market-tool invoker: 2xx 但 result.status=error 折叠为抛错且带 error detail', async () => {
  const invoke = createMarketToolInvoker({
    marketUrl: 'http://market.test/',
    getToken: () => 'sso-tok',
    fetchImpl: async () =>
      new Response(JSON.stringify({
        result: { tool: 'x', status: 'error', error: '超时未返回' }
      }), { status: 200 })
  })
  await assert.rejects(invoke('x', {}), (err: unknown) => {
    const message = err instanceof Error ? err.message : String(err)
    assert.ok(message.includes('执行失败'), message)
    assert.ok(message.includes('超时未返回'), message)
    return true
  })
})

test('market-tool invoker: 缺 token 抛中文 SSO 提示且不发请求', async () => {
  let called = false
  const invoke = createMarketToolInvoker({
    marketUrl: 'http://market.test',
    getToken: () => null,
    fetchImpl: async () => {
      called = true
      return new Response('{}', { status: 200 })
    }
  })
  await assert.rejects(invoke('x', {}), /SSO/)
  assert.ok(!called, '无 token 时不应发起请求')
})

test('market-tool invoker: 网络错误折叠并保留 cause', async () => {
  const netErr = new Error('fetch 请求失败') as Error & { cause?: unknown }
  netErr.cause = new Error('ECONNREFUSED')
  const invoke = createMarketToolInvoker({
    marketUrl: 'http://market.test',
    getToken: () => 'sso-tok',
    fetchImpl: async () => {
      throw netErr
    }
  })
  await assert.rejects(invoke('x', {}), /ECONNREFUSED/)
})
