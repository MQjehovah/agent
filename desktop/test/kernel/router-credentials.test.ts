import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRouterCredentials, type RouterCredentialIdentity } from '../../electron/main/router-credentials'

type FetchHandler = (url: string, init?: RequestInit) => Promise<Response>

interface Call {
  url: string
  init?: RequestInit
}

interface FakeState {
  identity: RouterCredentialIdentity | null
  clock: number
  saves: number
  ensureFreshCalls: number
  ensureFreshOidc: () => Promise<void>
  calls: Call[]
  handler: FetchHandler
}

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response
}

function setup(
  opts: { now?: number; identity?: Partial<RouterCredentialIdentity>; adminUrl?: string; clientSecret?: string } = {}
) {
  const state: FakeState = {
    identity: {
      user: { id: 'emp-1' },
      oidc: { idToken: 'id-token-1' },
      routerKey: '',
      ...opts.identity
    },
    clock: opts.now ?? 1_000_000,
    saves: 0,
    ensureFreshCalls: 0,
    ensureFreshOidc: async () => {
      state.ensureFreshCalls++
    },
    calls: [],
    handler: async () => jsonResponse({})
  }

  const creds = createRouterCredentials({
    getIdentity: () => state.identity,
    saveIdentity: (next) => {
      state.identity = next
      state.saves++
    },
    ensureFreshOidc: () => state.ensureFreshOidc(),
    issuer: () => 'https://auth.example.com',
    oidcClient: () => ({ clientId: 'dashboard-gateway', clientSecret: opts.clientSecret ?? 'oidc-secret' }),
    routerAdminUrl: () => opts.adminUrl ?? 'https://gw.example.com/',
    fetchImpl: (async (url: string, init?: RequestInit) => {
      state.calls.push({ url: String(url), init })
      return state.handler(String(url), init)
    }) as unknown as typeof fetch,
    now: () => state.clock
  })

  return { creds, state }
}

function authHeader(call: Call): string {
  return (call.init?.headers as Record<string, string>).Authorization
}

function bodyParams(call: Call): URLSearchParams {
  return new URLSearchParams(String(call.init?.body))
}

test('freshRouterToken: 未过期(余量 >60s)时不发请求直接返回', async () => {
  const { creds, state } = setup({ identity: { routerToken: 'rt-old', routerTokenExpiresAt: 1_000_000 + 10 * 60_000 } })
  assert.equal(await creds.freshRouterToken(), 'rt-old')
  assert.equal(state.calls.length, 0)
})

test('freshRouterToken: 过期时发起 RFC 8693 token-exchange,请求体正确并写回凭据', async () => {
  const { creds, state } = setup({ identity: { routerToken: 'rt-old', routerTokenExpiresAt: 999_000 } })
  state.handler = async () =>
    jsonResponse({
      access_token: 'rt-new',
      issued_token_type: 'urn:ietf:params:oauth:token-type:access_token',
      token_type: 'Bearer',
      expires_in: 3600,
      scope: 'usage'
    })

  assert.equal(await creds.freshRouterToken(), 'rt-new')
  assert.equal(state.ensureFreshCalls, 1)
  assert.equal(state.calls.length, 1)
  const [call] = state.calls
  assert.equal(call.url, 'https://auth.example.com/token')
  assert.equal(call.init?.method, 'POST')
  const body = bodyParams(call)
  assert.equal(body.get('grant_type'), 'urn:ietf:params:oauth:grant-type:token-exchange')
  assert.equal(body.get('subject_token'), 'id-token-1')
  assert.equal(body.get('subject_token_type'), 'urn:ietf:params:oauth:token-type:id_token')
  assert.equal(body.get('audience'), 'router')
  assert.equal(body.get('client_id'), 'dashboard-gateway')
  assert.equal(body.get('client_secret'), 'oidc-secret')
  assert.equal(state.identity?.routerToken, 'rt-new')
  assert.equal(state.identity?.routerTokenExpiresAt, 1_000_000 + 3_600_000)
  assert.equal(state.saves, 1)
})

test('freshRouterToken: 公共客户端(secret 为空)时交换请求体不含 client_secret', async () => {
  const { creds, state } = setup({ clientSecret: '' })
  state.handler = async () => jsonResponse({ access_token: 'rt-new', expires_in: 3600 })

  assert.equal(await creds.freshRouterToken(), 'rt-new')
  assert.equal(state.calls.length, 1)
  const body = bodyParams(state.calls[0])
  assert.equal(body.get('client_id'), 'dashboard-gateway')
  assert.equal(body.has('client_secret'), false)
  assert.equal(state.identity?.routerToken, 'rt-new')
})

test('freshRouterToken: 并发调用合并为一次交换(单飞)', async () => {
  const { creds, state } = setup()
  let release!: (r: Response) => void
  state.handler = () =>
    new Promise<Response>((resolve) => {
      release = resolve
    })

  const p1 = creds.freshRouterToken()
  const p2 = creds.freshRouterToken()
  await new Promise((r) => setTimeout(r, 0))
  assert.equal(state.calls.length, 1)

  release(jsonResponse({ access_token: 'rt-sf', expires_in: 3600 }))
  assert.deepEqual(await Promise.all([p1, p2]), ['rt-sf', 'rt-sf'])
  assert.equal(state.calls.length, 1)

  assert.equal(await creds.freshRouterToken(), 'rt-sf')
  assert.equal(state.calls.length, 1)
})

test('freshRouterToken: 交换失败但有未过期旧 token 时回退旧值', async () => {
  const now = 1_000_000
  const { creds, state } = setup({ now, identity: { routerToken: 'rt-stale', routerTokenExpiresAt: now + 30_000 } })
  state.handler = async () => jsonResponse({ error: 'server_error' }, 500)

  assert.equal(await creds.freshRouterToken(), 'rt-stale')
  assert.equal(state.calls.length, 1)
})

test('freshRouterToken: 无旧 token 且交换失败时抛“router token 交换失败(HTTP xxx)”', async () => {
  const { creds, state } = setup()
  state.handler = async () => jsonResponse({}, 500)
  await assert.rejects(() => creds.freshRouterToken(), /router token 交换失败\(HTTP 500\)/)
})

test('freshRouterToken: 交换响应缺少 access_token 时抛错', async () => {
  const { creds, state } = setup()
  state.handler = async () => jsonResponse({})
  await assert.rejects(() => creds.freshRouterToken(), /缺少 access_token/)
})

test('fetchRouterKey: 无 routerKey 时 GET /api/me/key 带 Bearer,成功后写回并持久化', async () => {
  const { creds, state } = setup({ identity: { routerToken: 'rt-1', routerTokenExpiresAt: 1_000_000 + 600_000 } })
  state.handler = async () => jsonResponse({ key: 'sk-abc', keyId: 7 })

  assert.equal(await creds.fetchRouterKey(), 'sk-abc')
  assert.equal(state.calls.length, 1)
  assert.equal(state.calls[0].url, 'https://gw.example.com/api/me/key')
  assert.equal(authHeader(state.calls[0]), 'Bearer rt-1')
  assert.equal(state.identity?.routerKey, 'sk-abc')
  assert.equal(state.saves, 1)
})

test('fetchRouterKey: 已有 routerKey 时直接返回且不发请求', async () => {
  const { creds, state } = setup({ identity: { routerKey: 'sk-cached' } })
  assert.equal(await creds.fetchRouterKey(), 'sk-cached')
  assert.equal(state.calls.length, 0)
})

test('fetchRouterKey: 401 时把 routerTokenExpiresAt 置 0 重换 token 并重试一次成功', async () => {
  const now = 1_000_000
  const { creds, state } = setup({ now, identity: { routerToken: 'rt-old', routerTokenExpiresAt: now + 600_000 } })
  let keyCalls = 0
  state.handler = async (url) => {
    if (url.endsWith('/token')) return jsonResponse({ access_token: 'rt-new', expires_in: 3600 })
    keyCalls++
    return keyCalls === 1 ? jsonResponse({ detail: 'expired' }, 401) : jsonResponse({ key: 'sk-new' })
  }

  assert.equal(await creds.fetchRouterKey(), 'sk-new')
  assert.equal(keyCalls, 2)
  assert.equal(state.calls.filter((c) => c.url.endsWith('/token')).length, 1)
  assert.equal(state.calls.length, 3)
  const keyReqs = state.calls.filter((c) => c.url.endsWith('/api/me/key'))
  assert.equal(authHeader(keyReqs[0]), 'Bearer rt-old')
  assert.equal(authHeader(keyReqs[1]), 'Bearer rt-new')
  assert.equal(state.identity?.routerKey, 'sk-new')
})

test('fetchRouterKey: 重试仍 401 时只重试一次并抛错', async () => {
  const now = 1_000_000
  const { creds, state } = setup({ now, identity: { routerToken: 'rt-old', routerTokenExpiresAt: now + 600_000 } })
  let keyCalls = 0
  state.handler = async (url) => {
    if (url.endsWith('/token')) return jsonResponse({ access_token: 'rt-new', expires_in: 3600 })
    keyCalls++
    return jsonResponse({ detail: 'nope' }, 401)
  }

  await assert.rejects(() => creds.fetchRouterKey(), /企业认证已过期,请重新登录企业账号/)
  assert.equal(keyCalls, 2)
})

test('fetchRouterKey: 非 2xx 抛出带状态码的错误且不写回', async () => {
  const { creds, state } = setup({ identity: { routerToken: 'rt-1', routerTokenExpiresAt: 1_000_000 + 600_000 } })
  state.handler = async () => jsonResponse({}, 500)
  await assert.rejects(() => creds.fetchRouterKey(), /router key 获取失败\(HTTP 500\)/)
  assert.equal(state.identity?.routerKey, '')
})

test('未登录时 freshRouterToken / fetchRouterKey 抛中文提示', async () => {
  const { creds, state } = setup()
  state.identity = null
  await assert.rejects(() => creds.freshRouterToken(), /请先完成企业 SSO 登录/)
  await assert.rejects(() => creds.fetchRouterKey(), /请先完成企业 SSO 登录/)
  assert.equal(state.calls.length, 0)
})

test('竞态: 交换悬挂期间切换身份,新身份不会 join 旧 inflight 拿到旧用户 token', async () => {
  const { creds, state } = setup()
  // 受控顺序:emp-1 的 /token 挂起(旧用户),emp-2 的 /token 立即成功(新用户)
  let releaseOld!: (r: Response) => void
  state.handler = async (url, init) => {
    if (url.endsWith('/token')) {
      const subject = new URLSearchParams(String(init?.body)).get('subject_token')
      if (subject === 'id-token-1') {
        return new Promise<Response>((resolve) => {
          releaseOld = resolve
        })
      }
      return jsonResponse({ access_token: 'rt-new-user', expires_in: 3600 })
    }
    const auth = (init?.headers as Record<string, string>).Authorization
    return jsonResponse({ key: auth === 'Bearer rt-new-user' ? 'sk-new-user' : 'sk-other-user' })
  }

  // emp-1 发起交换并悬挂
  const oldPromise = creds.freshRouterToken()
  await new Promise((r) => setTimeout(r, 0))
  assert.equal(state.calls.length, 1)

  // 模拟重新登录为 emp-2(旧请求仍在途)
  state.identity = { user: { id: 'emp-2' }, oidc: { idToken: 'id-token-2' }, routerKey: '' }

  // 新身份应另起交换,而不是 join 旧 inflight
  assert.equal(await creds.freshRouterToken(), 'rt-new-user')
  assert.equal(state.identity.routerToken, 'rt-new-user')

  // 放行旧请求:旧 token 不得写入新身份,也不得返回给等待者
  releaseOld(jsonResponse({ access_token: 'rt-old-user', expires_in: 3600 }))
  await assert.rejects(() => oldPromise, /身份已变更,取消写入/)
  assert.equal(state.identity?.routerToken, 'rt-new-user')
  assert.notEqual(state.identity?.routerToken, 'rt-old-user')

  // 后续取 key 也必须基于新身份的 token
  assert.equal(await creds.fetchRouterKey(), 'sk-new-user')
  assert.equal(state.identity?.routerKey, 'sk-new-user')
})

test('竞态: 交换期间身份消失时抛错,不返回未持久化的 token', async () => {
  const { creds, state } = setup()
  let release!: (r: Response) => void
  state.handler = () =>
    new Promise<Response>((resolve) => {
      release = resolve
    })

  const promise = creds.freshRouterToken()
  await new Promise((r) => setTimeout(r, 0))
  assert.equal(state.calls.length, 1)

  state.identity = null
  release(jsonResponse({ access_token: 'rt-ghost', expires_in: 3600 }))

  await assert.rejects(() => promise, /身份已变更,取消写入/)
  assert.equal(state.saves, 0)
  assert.equal(state.identity, null)
})

test('竞态: /api/me/key 响应挂起期间换身份,旧 sk- 不写回新身份', async () => {
  const { creds, state } = setup({ identity: { routerToken: 'rt-old-user', routerTokenExpiresAt: 1_000_000 + 600_000 } })
  let releaseJson!: (body: unknown) => void
  state.handler = async (url, init) => {
    const auth = (init?.headers as Record<string, string>).Authorization
    if (auth === 'Bearer rt-old-user') {
      return {
        ok: true,
        status: 200,
        json: () =>
          new Promise((resolve) => {
            releaseJson = resolve
          })
      } as unknown as Response
    }
    return jsonResponse({ key: 'sk-new-user' })
  }

  const promise = creds.fetchRouterKey()
  await new Promise((r) => setTimeout(r, 0))
  assert.equal(state.calls.length, 1)

  // 响应尚未解析时完成重新登录
  state.identity = { user: { id: 'emp-2' }, oidc: { idToken: 'id-token-2' }, routerKey: '', routerToken: 'rt-new-user', routerTokenExpiresAt: 1_000_000 + 600_000 }
  releaseJson({ key: 'sk-old-user' })

  await assert.rejects(() => promise, /身份已变更,取消写入/)
  assert.equal(state.identity?.routerKey, '')
  assert.equal(state.saves, 0)

  // 后续新身份的正常调用不受影响
  assert.equal(await creds.fetchRouterKey(), 'sk-new-user')
  assert.equal(state.identity?.routerKey, 'sk-new-user')
})

test('竞态: 401 挂起期间换身份,不清零新身份的过期时间且不重试', async () => {
  const now = 1_000_000
  const { creds, state } = setup({ now, identity: { routerToken: 'rt-old-user', routerTokenExpiresAt: now + 600_000 } })
  let release401!: (r: Response) => void
  let keyCalls = 0
  state.handler = async (url) => {
    if (url.endsWith('/token')) return jsonResponse({ access_token: 'rt-refreshed', expires_in: 3600 })
    keyCalls++
    if (keyCalls === 1) {
      return new Promise<Response>((resolve) => {
        release401 = resolve
      })
    }
    return jsonResponse({ key: 'sk-new-user' })
  }

  const promise = creds.fetchRouterKey()
  await new Promise((r) => setTimeout(r, 0))
  assert.equal(state.calls.length, 1)

  // 401 响应尚未到达时完成重新登录
  state.identity = {
    user: { id: 'emp-2' },
    oidc: { idToken: 'id-token-2' },
    routerKey: '',
    routerToken: 'rt-new-user',
    routerTokenExpiresAt: now + 1_200_000
  }
  release401(jsonResponse({ detail: 'expired' }, 401))

  await assert.rejects(() => promise, /身份已变更,取消写入/)
  assert.equal(state.identity?.routerTokenExpiresAt, now + 1_200_000)
  assert.equal(state.saves, 0)
  assert.equal(state.calls.filter((c) => c.url.endsWith('/token')).length, 0)
  assert.equal(keyCalls, 1)
})

test('exchange: ensureFreshOidc 失败且无旧 token 时包成可读登录失效文案', async () => {
  const { creds, state } = setup()
  state.ensureFreshOidc = async () => {
    throw new Error('刷新失败(HTTP 400)')
  }
  await assert.rejects(
    () => creds.freshRouterToken(),
    /企业登录状态已失效,请重新登录企业账号\(刷新失败\(HTTP 400\)\)/
  )
  assert.equal(state.calls.length, 0)
})

test('ensureFreshOidc 失败但有未过期旧 token 时回退旧值', async () => {
  const now = 1_000_000
  const { creds, state } = setup({ now, identity: { routerToken: 'rt-old', routerTokenExpiresAt: now + 30_000 } })
  state.ensureFreshOidc = async () => {
    throw new Error('刷新失败(HTTP 400)')
  }
  assert.equal(await creds.freshRouterToken(), 'rt-old')
  assert.equal(state.calls.length, 0)
})

test('余量边界: 恰好 now+60s 视为需交换; expires_in 缺省按 3600', async () => {
  const now = 1_000_000
  const { creds, state } = setup({ now, identity: { routerToken: 'rt-edge', routerTokenExpiresAt: now + 60_000 } })
  state.handler = async () => jsonResponse({ access_token: 'rt-fresh' })
  assert.equal(await creds.freshRouterToken(), 'rt-fresh')
  assert.equal(state.calls.length, 1)
  assert.equal(state.identity?.routerTokenExpiresAt, now + 3_600_000)
})

test('网络异常: token 交换包装为可读文案', async () => {
  const { creds, state } = setup()
  state.handler = async () => {
    throw new Error('ECONNREFUSED')
  }
  await assert.rejects(() => creds.freshRouterToken(), /router token 交换失败\(网络异常\)/)
})

test('网络异常: key 获取包装为可读文案', async () => {
  const { creds, state } = setup({ identity: { routerToken: 'rt-1', routerTokenExpiresAt: 1_000_000 + 600_000 } })
  state.handler = async () => {
    throw new Error('ECONNREFUSED')
  }
  await assert.rejects(() => creds.fetchRouterKey(), /router key 获取失败\(网络异常\)/)
})

test('fetchRouterKey: 未配置 routerAdminUrl 时直接抛中文错误且不发请求', async () => {
  const { creds, state } = setup({
    adminUrl: '  ',
    identity: { routerToken: 'rt-1', routerTokenExpiresAt: 1_000_000 + 600_000 }
  })
  await assert.rejects(() => creds.fetchRouterKey(), /未配置路由管理端地址/)
  assert.equal(state.calls.length, 0)
})

test('fetchRouterKey: 重试后 403 映射为未开通文案', async () => {
  const now = 1_000_000
  const { creds, state } = setup({ now, identity: { routerToken: 'rt-old', routerTokenExpiresAt: now + 600_000 } })
  let keyCalls = 0
  state.handler = async (url) => {
    if (url.endsWith('/token')) return jsonResponse({ access_token: 'rt-new', expires_in: 3600 })
    keyCalls++
    return keyCalls === 1 ? jsonResponse({}, 401) : jsonResponse({ detail: '未开通' }, 403)
  }
  await assert.rejects(
    () => creds.fetchRouterKey(),
    (err: Error & { status?: number }) => {
      assert.match(err.message, /企业账号未开通算力网关,请联系管理员/)
      assert.match(err.message, /未开通/)
      assert.equal(err.status, 403)
      return true
    }
  )
})

test('同身份并发 fetchRouterKey 遇 401 时单飞重换并各自成功', async () => {
  const now = 1_000_000
  const { creds, state } = setup({ now, identity: { routerToken: 'rt-1', routerTokenExpiresAt: now + 600_000 } })
  let tokenCalls = 0
  let keyCalls = 0
  state.handler = async (url) => {
    if (url.endsWith('/token')) {
      tokenCalls++
      return jsonResponse({ access_token: 'rt-2', expires_in: 3600 })
    }
    keyCalls++
    return keyCalls <= 2 ? jsonResponse({ detail: 'expired' }, 401) : jsonResponse({ key: 'sk-1' })
  }

  const [a, b] = await Promise.all([creds.fetchRouterKey(), creds.fetchRouterKey()])
  assert.equal(a, 'sk-1')
  assert.equal(b, 'sk-1')
  assert.equal(tokenCalls, 1)
  assert.equal(keyCalls, 4)
  assert.equal(state.identity?.routerKey, 'sk-1')
})
