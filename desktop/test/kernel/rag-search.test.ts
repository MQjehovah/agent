import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRagSearcher } from '../../electron/main/kernel/rag'

/** 记录一次 fetch 调用的地址与请求配置 */
interface CapturedCall {
  input: string
  init: RequestInit
}

test('rag search posts JSON to ragUrl/api/search with bearer token and returns parsed json', async () => {
  const calls: CapturedCall[] = []
  const search = createRagSearcher({
    ragUrl: 'http://rag.test/',
    getToken: () => 'tok',
    fetchImpl: async (input, init) => {
      calls.push({ input: String(input), init: init ?? {} })
      return new Response(JSON.stringify({ results: [{ id: '1', title: '报销制度' }], total: 1 }), { status: 200 })
    }
  })
  const payload = await search('/api/search', { query: '报销制度', top_k: 3 })
  assert.equal(calls.length, 1)
  assert.equal(calls[0].input, 'http://rag.test/api/search')
  assert.equal(calls[0].init.method, 'POST')
  assert.deepEqual(calls[0].init.headers, {
    authorization: 'Bearer tok',
    'content-type': 'application/json'
  })
  assert.equal(calls[0].init.body, JSON.stringify({ query: '报销制度', top_k: 3 }))
  assert.deepEqual(payload, { results: [{ id: '1', title: '报销制度' }], total: 1 })
})

test('rag search rejects with SSO hint when token is missing without calling fetch', async () => {
  let called = false
  const search = createRagSearcher({
    ragUrl: 'http://rag.test',
    getToken: () => null,
    fetchImpl: async () => {
      called = true
      return new Response('{}', { status: 200 })
    }
  })
  await assert.rejects(search('/api/search', { query: 'q' }), (err: unknown) => {
    const message = err instanceof Error ? err.message : String(err)
    assert.ok(message.includes('SSO'), `错误文案应含 SSO 提示: ${message}`)
    assert.ok(/[\u4e00-\u9fff]/.test(message), `错误文案应为中文: ${message}`)
    assert.ok(!called, '无 token 时不应发起请求')
    return true
  })
})

test('rag search surfaces detail field from JSON error body instead of raw body', async () => {
  const search = createRagSearcher({
    ragUrl: 'http://rag.test',
    getToken: () => 'tok',
    fetchImpl: async () =>
      new Response(
        JSON.stringify({ detail: '知识库额度校验未通过', stack: 'at Foo (x.js:1:1)\n  at Bar (y.js:2:2)' }),
        { status: 403 }
      )
  })
  await assert.rejects(search('/api/search', { query: 'q' }), (err: unknown) => {
    const message = err instanceof Error ? err.message : String(err)
    assert.ok(message.includes('403'), message)
    assert.ok(message.includes('知识库额度校验未通过'), message)
    assert.ok(!message.includes('at Foo'), `错误文案不应含原文堆栈: ${message}`)
    return true
  })
})

test('rag search truncates non-JSON error body into message', async () => {
  const tail = 'y'.repeat(1000)
  const search = createRagSearcher({
    ragUrl: 'http://rag.test',
    getToken: () => 'tok',
    fetchImpl: async () => new Response(`<html>服务器内部错误</html>${tail}`, { status: 500 })
  })
  await assert.rejects(search('/api/search', { query: 'q' }), (err: unknown) => {
    const message = err instanceof Error ? err.message : String(err)
    assert.ok(message.includes('500'), message)
    assert.ok(message.includes('<html>服务器内部错误</html>'), message)
    assert.ok(!message.includes('y'.repeat(250)), `错误文案应截断正文而不含全文: ${message.slice(0, 240)}`)
    return true
  })
})

test('rag search folds network error and its cause into message', async () => {
  const netErr = new Error('fetch 请求失败') as Error & { cause?: unknown }
  netErr.cause = new Error('ECONNREFUSED 127.0.0.1:8092')
  const search = createRagSearcher({
    ragUrl: 'http://rag.test',
    getToken: () => 'tok',
    fetchImpl: async () => {
      throw netErr
    }
  })
  await assert.rejects(search('/api/search', { query: 'q' }), (err: unknown) => {
    const message = err instanceof Error ? err.message : String(err)
    assert.ok(message.includes('fetch 请求失败'), message)
    assert.ok(message.includes('ECONNREFUSED'), `错误文案应带 cause: ${message}`)
    return true
  })
})
