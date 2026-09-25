import { test } from 'node:test'
import assert from 'node:assert/strict'
import { fetchUsageSummary, parseUsageSummary } from '../../electron/main/usage'

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response
}

const RAW = {
  balance: 12.5,
  rateLimit: 60,
  quota: { daily: 100000, monthly: 3000000 },
  today: { tokensIn: 100, tokensOut: 50, tokens: 150, cost: 1.25 },
  month: { tokensIn: 600, tokensOut: 300, tokens: 900, cost: 7.5 },
  models: [
    {
      name: 'm1',
      today: { tokensIn: 10, tokensOut: 20, tokens: 30, cost: 0.1 },
      month: { tokensIn: 40, tokensOut: 50, tokens: 90, cost: 0.2 },
      dailyQuota: 1000,
      monthlyQuota: 2000
    }
  ],
  truncated: false,
  fetchedAt: '2026-09-22T00:00:00.000Z'
}

test('parseUsageSummary: 服务端同构 JSON(含 fetchedAt)原样映射', () => {
  assert.deepEqual(parseUsageSummary(JSON.parse(JSON.stringify(RAW))), RAW)
})

test('parseUsageSummary: 字符串数字数值化,tokens 缺失时按 in+out 兜底', () => {
  const s = parseUsageSummary({
    balance: '12.5',
    rateLimit: '60',
    quota: { daily: '100000', monthly: '3000000' },
    today: { tokensIn: '100', tokensOut: '50', cost: '1.25' },
    month: { tokensIn: '600', tokensOut: '300', tokens: '900', cost: '7.50' },
    models: [{ name: 'm1', today: { tokensIn: '10', tokensOut: '20' }, month: {}, dailyQuota: '1000', monthlyQuota: '2000' }],
    truncated: false,
    fetchedAt: '2026-09-22T00:00:00.000Z'
  })
  assert.equal(s.balance, 12.5)
  assert.equal(s.rateLimit, 60)
  assert.deepEqual(s.quota, { daily: 100000, monthly: 3000000 })
  assert.deepEqual(s.today, { tokensIn: 100, tokensOut: 50, tokens: 150, cost: 1.25 })
  assert.deepEqual(s.month, { tokensIn: 600, tokensOut: 300, tokens: 900, cost: 7.5 })
  assert.equal(s.models[0].today.tokens, 30)
  assert.equal(s.models[0].month.tokens, 0)
  assert.equal(s.models[0].dailyQuota, 1000)
})

test('parseUsageSummary: 缺 fetchedAt 时本地补,非对象/缺字段给出安全默认值', () => {
  const a = parseUsageSummary({ balance: 1 })
  assert.equal(a.balance, 1)
  assert.deepEqual(a.today, { tokensIn: 0, tokensOut: 0, tokens: 0, cost: 0 })
  assert.deepEqual(a.models, [])
  assert.equal(a.truncated, false)
  assert.ok(a.fetchedAt && !Number.isNaN(Date.parse(a.fetchedAt)))

  const b = parseUsageSummary(null)
  assert.equal(b.balance, 0)
  assert.equal(b.models.length, 0)
  assert.ok(b.fetchedAt)
})

test('fetchUsageSummary: 只发 1 个 GET /api/me/usage 请求且带 Bearer router token', async () => {
  const seen: Array<{ url: string; init?: RequestInit }> = []
  let tokenCalls = 0
  const fetchImpl = (async (url: string, init?: RequestInit) => {
    seen.push({ url: String(url), init })
    return jsonResponse(JSON.parse(JSON.stringify(RAW)))
  }) as unknown as typeof fetch

  const summary = await fetchUsageSummary({
    baseUrl: 'https://gw.example.com/',
    token: async () => {
      tokenCalls++
      return 'rt-1'
    },
    fetchImpl
  })

  assert.equal(seen.length, 1)
  assert.equal(tokenCalls, 1)
  assert.equal(seen[0].url, 'https://gw.example.com/api/me/usage')
  assert.equal(seen[0].init?.method ?? 'GET', 'GET')
  assert.equal((seen[0].init?.headers as Record<string, string>).Authorization, 'Bearer rt-1')
  assert.deepEqual(summary, RAW)
})

test('fetchUsageSummary: 401 映射为“重新登录企业账号”', async () => {
  const fetchImpl = (async () => jsonResponse({ detail: 'expired' }, 401)) as unknown as typeof fetch
  await assert.rejects(
    () => fetchUsageSummary({ baseUrl: 'https://gw.example.com', token: async () => 'rt', fetchImpl }),
    (err: Error & { status?: number }) => {
      assert.match(err.message, /企业认证已过期,请重新登录企业账号/)
      assert.equal(err.status, 401)
      return true
    }
  )
})

test('fetchUsageSummary: 403 映射为“未开通算力网关”并带 detail', async () => {
  const fetchImpl = (async () => jsonResponse({ detail: '企业未购买该服务' }, 403)) as unknown as typeof fetch
  await assert.rejects(
    () => fetchUsageSummary({ baseUrl: 'https://gw.example.com', token: async () => 'rt', fetchImpl }),
    (err: Error & { status?: number }) => {
      assert.match(err.message, /企业账号未开通算力网关,请联系管理员/)
      assert.match(err.message, /企业未购买该服务/)
      assert.equal(err.status, 403)
      return true
    }
  )
})

test('fetchUsageSummary: 首次 401 时重换 token 并重试一次成功', async () => {
  const auths: string[] = []
  let tokenCalls = 0
  let refreshCalls = 0
  const fetchImpl = (async (_url: string, init?: RequestInit) => {
    auths.push((init?.headers as Record<string, string>).Authorization)
    return auths.length === 1
      ? jsonResponse({ detail: 'expired' }, 401)
      : jsonResponse(JSON.parse(JSON.stringify(RAW)))
  }) as unknown as typeof fetch

  const summary = await fetchUsageSummary({
    baseUrl: 'https://gw.example.com',
    token: async () => {
      tokenCalls++
      return 'rt-old'
    },
    refreshToken: async () => {
      refreshCalls++
      return 'rt-new'
    },
    fetchImpl
  })

  assert.equal(tokenCalls, 1)
  assert.equal(refreshCalls, 1)
  assert.deepEqual(auths, ['Bearer rt-old', 'Bearer rt-new'])
  assert.deepEqual(summary, RAW)
})

test('fetchUsageSummary: 重试仍 401 抛重登文案且只重换一次', async () => {
  let refreshCalls = 0
  let requested = 0
  const fetchImpl = (async () => {
    requested++
    return jsonResponse({ detail: 'expired' }, 401)
  }) as unknown as typeof fetch
  await assert.rejects(
    () =>
      fetchUsageSummary({
        baseUrl: 'https://gw.example.com',
        token: async () => 'rt-old',
        refreshToken: async () => {
          refreshCalls++
          return 'rt-new'
        },
        fetchImpl
      }),
    /企业认证已过期,请重新登录企业账号/
  )
  assert.equal(refreshCalls, 1)
  assert.equal(requested, 2)
})

test('fetchUsageSummary: 其它非 2xx 映射为 HTTP 状态码文案', async () => {
  const fetchImpl = (async () => jsonResponse({}, 500)) as unknown as typeof fetch
  await assert.rejects(
    () => fetchUsageSummary({ baseUrl: 'https://gw.example.com', token: async () => 'rt', fetchImpl }),
    /网关管理端返回 HTTP 500/
  )
})

test('fetchUsageSummary: 未配置 routerAdminUrl 时直接抛中文错误且不发请求', async () => {
  let requested = 0
  const fetchImpl = (async () => {
    requested++
    return jsonResponse({})
  }) as unknown as typeof fetch
  await assert.rejects(
    () => fetchUsageSummary({ baseUrl: '  ', token: async () => 'rt', fetchImpl }),
    (err: Error) => {
      assert.match(err.message, /未配置路由管理端地址/)
      assert.doesNotMatch(err.message, /设置页/)
      return true
    }
  )
  assert.equal(requested, 0)
})
