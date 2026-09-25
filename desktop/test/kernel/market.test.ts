import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createMarketClient } from '../../electron/main/kernel/market'

/** 记录一次 fetch 调用的地址与请求配置(仅取测试关心的字段) */
interface CapturedCall {
  input: string
  init: {
    method?: string
    headers?: Record<string, unknown>
    body?: string
  }
}

/** 组装一个假 fetch：记录调用并按 handler 返回 Response；返回类型对齐 typeof fetch */
function stubFetch(calls: CapturedCall[], handler: () => Response): typeof fetch {
  return async (input, init) => {
    const req = init ?? {}
    calls.push({
      input: String(input),
      init: {
        method: req.method,
        headers: req.headers as Record<string, unknown> | undefined,
        body: req.body !== undefined ? String(req.body) : undefined
      }
    })
    return handler()
  }
}

const JSON_HEADERS = { authorization: 'Bearer sso-tok', 'content-type': 'application/json' }

test('market client listMy 请求我的能力并映射最小字段、丢弃不支持类型', async () => {
  const calls: CapturedCall[] = []
  const raw = [
    {
      id: 'cap_1',
      name: 'doc-check',
      description: '检查文档规范',
      type: 'skill',
      version: '1.2.0',
      status: 'published',
      author_name: 'zhangsan',
      added: true,
      owned: false,
      extra: '忽略的字段'
    },
    // 非四类能力（workflow）dashboard 不支持本地安装，列表应丢弃
    { id: 'wf_1', name: 'report-flow', type: 'workflow', version: '0.1.0', description: '' },
    // 缺关键字段的条目也容错丢弃
    { name: 'no-id', type: 'skill' }
  ]
  const client = createMarketClient({
    marketUrl: 'http://market.test/',
    getToken: () => 'sso-tok',
    fetchImpl: stubFetch(calls, () => new Response(JSON.stringify(raw), { status: 200 }))
  })
  const caps = await client.listMy()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].input, 'http://market.test/api/my/capabilities?scope=added')
  assert.equal(calls[0].init.method, 'GET')
  assert.deepEqual(calls[0].init.headers, JSON_HEADERS)
  assert.equal(caps.length, 1)
  assert.deepEqual(caps[0], {
    id: 'cap_1',
    name: 'doc-check',
    type: 'skill',
    version: '1.2.0',
    description: '检查文档规范',
    status: 'published',
    author_name: 'zhangsan',
    added: true,
    owned: false
  })
})

test('market client listMy 映射 runtime（只收 cloud/local/recommended，非法/空对象丢弃）', async () => {
  const calls: CapturedCall[] = []
  const raw = [
    {
      id: 'cap_r1',
      name: 'dingtalk',
      type: 'mcp',
      version: '1.0.0',
      runtime: {
        cloud: true,
        local: false,
        recommended: 'cloud',
        transport: 'stdio',
        command: 'python',
        tool_count: 61
      }
    },
    { id: 'cap_r2', name: 'local-mcp', type: 'mcp', version: '1.0.0', runtime: { local: true } },
    { id: 'cap_r3', name: 'bad-runtime', type: 'skill', version: '1.0.0', runtime: 'oops' },
    { id: 'cap_r4', name: 'empty-runtime', type: 'skill', version: '1.0.0', runtime: {} }
  ]
  const client = createMarketClient({
    marketUrl: 'http://market.test',
    getToken: () => 'sso-tok',
    fetchImpl: stubFetch(calls, () => new Response(JSON.stringify(raw), { status: 200 }))
  })
  const caps = await client.listMy()
  assert.equal(caps.length, 4)
  assert.deepEqual(caps[0].runtime, { cloud: true, local: false, recommended: 'cloud' })
  assert.deepEqual(caps[1].runtime, { local: true })
  assert.equal(caps[2].runtime, undefined)
  assert.equal(caps[3].runtime, undefined)
})

test('market client listMy 支持显式 scope 参数', async () => {
  const calls: CapturedCall[] = []
  const client = createMarketClient({
    marketUrl: 'http://market.test',
    getToken: () => 'sso-tok',
    fetchImpl: stubFetch(calls, () => new Response('[]', { status: 200 }))
  })
  await client.listMy('owned')
  assert.equal(calls[0].input, 'http://market.test/api/my/capabilities?scope=owned')
})

test('market client subscribe POST capability_id 到我的能力', async () => {
  const calls: CapturedCall[] = []
  const client = createMarketClient({
    marketUrl: 'http://market.test/',
    getToken: () => 'sso-tok',
    fetchImpl: stubFetch(calls, () => new Response(JSON.stringify({ message: '已加入我的能力' }), { status: 201 }))
  })
  const result = await client.subscribe('cap_9')
  assert.equal(result, undefined)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].input, 'http://market.test/api/my/capabilities')
  assert.equal(calls[0].init.method, 'POST')
  assert.deepEqual(calls[0].init.headers, JSON_HEADERS)
  assert.equal(calls[0].init.body, JSON.stringify({ capability_id: 'cap_9' }))
})

test('market client unsubscribe DELETE 我的能力对应条目(带 URL 编码)', async () => {
  const calls: CapturedCall[] = []
  const client = createMarketClient({
    marketUrl: 'http://market.test/',
    getToken: () => 'sso-tok',
    fetchImpl: stubFetch(calls, () => new Response(JSON.stringify({ message: '已从我的能力移除' }), { status: 200 }))
  })
  const result = await client.unsubscribe('cap id/1')
  assert.equal(result, undefined)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].input, 'http://market.test/api/my/capabilities/cap%20id%2F1')
  assert.equal(calls[0].init.method, 'DELETE')
  assert.equal(
    (calls[0].init.headers as Record<string, string> | undefined)?.authorization,
    'Bearer sso-tok'
  )
})

test('market client download 返回 artifact 原始字节(带 version 查询)', async () => {
  const calls: CapturedCall[] = []
  const payload = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 1, 2, 3])
  const client = createMarketClient({
    marketUrl: 'http://market.test',
    getToken: () => 'sso-tok',
    fetchImpl: stubFetch(calls, () => new Response(payload, { status: 200 }))
  })
  const buf = await client.download('doc-check', '1.2.0')
  assert.equal(calls.length, 1)
  assert.equal(calls[0].input, 'http://market.test/api/capabilities/doc-check/download?version=1.2.0')
  assert.equal(calls[0].init.method, 'GET')
  assert.deepEqual(buf, Buffer.from(payload))
})

test('market client download 省略 version 时 URL 不带查询', async () => {
  const calls: CapturedCall[] = []
  const client = createMarketClient({
    marketUrl: 'http://market.test/',
    getToken: () => 'sso-tok',
    fetchImpl: stubFetch(calls, () => new Response(new Uint8Array([1]), { status: 200 }))
  })
  await client.download('doc-check')
  assert.equal(calls[0].input, 'http://market.test/api/capabilities/doc-check/download')
})

test('market client 缺 token 时抛中文 SSO 提示且不发起请求', async () => {
  let called = false
  const client = createMarketClient({
    marketUrl: 'http://market.test',
    getToken: () => null,
    fetchImpl: async () => {
      called = true
      return new Response('{}', { status: 200 })
    }
  })
  await assert.rejects(client.listMy(), (err: unknown) => {
    const message = err instanceof Error ? err.message : String(err)
    assert.ok(message.includes('SSO'), `错误文案应含 SSO 提示: ${message}`)
    assert.ok(/[\u4e00-\u9fff]/.test(message), `错误文案应为中文: ${message}`)
    assert.ok(!called, '无 token 时不应发起请求')
    return true
  })
})

test('market client 非 2xx 时取 detail 文案并带状态码', async () => {
  const client = createMarketClient({
    marketUrl: 'http://market.test',
    getToken: () => 'sso-tok',
    fetchImpl: async () =>
      new Response(JSON.stringify({ detail: '能力不存在或未发布', stack: 'at F(x.js:1)' }), { status: 404 })
  })
  await assert.rejects(client.download('ghost-cap'), (err: unknown) => {
    const message = err instanceof Error ? err.message : String(err)
    assert.ok(message.includes('404'), message)
    assert.ok(message.includes('能力不存在或未发布'), message)
    assert.ok(!message.includes('at F('), `错误文案不应含堆栈: ${message}`)
    return true
  })
})

test('market client 网络错误折叠并保留 cause', async () => {
  const netErr = new Error('fetch 请求失败') as Error & { cause?: unknown }
  netErr.cause = new Error('ECONNREFUSED 192.168.31.34:8093')
  const client = createMarketClient({
    marketUrl: 'http://market.test',
    getToken: () => 'sso-tok',
    fetchImpl: async () => {
      throw netErr
    }
  })
  await assert.rejects(client.subscribe('cap_1'), (err: unknown) => {
    const message = err instanceof Error ? err.message : String(err)
    assert.ok(message.includes('fetch 请求失败'), message)
    assert.ok(message.includes('ECONNREFUSED'), `错误文案应带 cause: ${message}`)
    return true
  })
})
