import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createPkcePair, createSsoFlow } from '../../electron/main/sso-flow'

type FetchHandler = (url: string, init?: RequestInit) => Promise<Response>

interface Call {
  url: string
  init?: RequestInit
}

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response
}

function setup(opts: { clientSecret?: string; deferred?: boolean; openExternalFails?: boolean } = {}) {
  const state = {
    calls: [] as Call[],
    opened: [] as string[],
    callbackStates: [] as string[],
    callbackTimeouts: [] as number[],
    pendingCodes: [] as Array<(code: string) => void>,
    closed: 0,
    handler: (async () =>
      jsonResponse({
        id_token: 'id-token-1',
        access_token: 'access-token-1',
        refresh_token: 'refresh-token-1',
        expires_in: 3600
      })) as FetchHandler
  }

  const flow = createSsoFlow({
    issuer: () => 'https://auth.example.com',
    oidcClient: () => ({ clientId: 'dashboard-gateway', clientSecret: opts.clientSecret ?? '' }),
    redirectUri: () => 'http://127.0.0.1:8090/api/auth/oidc/callback',
    openExternal: async (url) => {
      state.opened.push(url)
      if (opts.openExternalFails) throw new Error('openExternal 失败')
    },
    beginCallback: async (stateParam, timeoutMs) => {
      state.callbackStates.push(stateParam)
      state.callbackTimeouts.push(timeoutMs)
      const code = opts.deferred
        ? new Promise<string>((resolve) => {
            state.pendingCodes.push(resolve)
          })
        : Promise.resolve('auth-code-1')
      return {
        code,
        close: () => {
          state.closed++
        }
      }
    },
    fetchImpl: (async (url: string, init?: RequestInit) => {
      state.calls.push({ url: String(url), init })
      return state.handler(String(url), init)
    }) as unknown as typeof fetch
  })

  return { flow, state }
}

function authorizeParams(state: { opened: string[] }): URLSearchParams {
  return new URL(state.opened[0]).searchParams
}

function bodyParams(call: Call): URLSearchParams {
  return new URLSearchParams(String(call.init?.body))
}

test('createPkcePair: verifier 为 43 字符 base64url,challenge = base64url(sha256(verifier)),method S256', () => {
  const pair = createPkcePair()
  assert.match(pair.codeVerifier, /^[A-Za-z0-9_-]{43}$/)
  assert.match(pair.codeChallenge, /^[A-Za-z0-9_-]{43}$/)
  assert.equal(pair.codeChallengeMethod, 'S256')
  assert.equal(pair.codeChallenge, createHash('sha256').update(pair.codeVerifier).digest('base64url'))
})

test('authorize: 授权 URL 带 43 字符 code_challenge 与 code_challenge_method=S256,state 传入回环回调', async () => {
  const { flow, state } = setup()
  const tokens = await flow.authorize()

  assert.equal(tokens.access_token, 'access-token-1')
  assert.equal(state.opened.length, 1)
  const url = new URL(state.opened[0])
  assert.equal(`${url.origin}${url.pathname}`, 'https://auth.example.com/authorize')
  const params = url.searchParams
  assert.equal(params.get('response_type'), 'code')
  assert.equal(params.get('client_id'), 'dashboard-gateway')
  assert.equal(params.get('redirect_uri'), 'http://127.0.0.1:8090/api/auth/oidc/callback')
  assert.match(params.get('state') ?? '', /^[0-9a-f]{32}$/)
  assert.equal(params.get('scope'), 'openid profile')
  assert.match(params.get('code_challenge') ?? '', /^[A-Za-z0-9_-]{43}$/)
  assert.equal(params.get('code_challenge_method'), 'S256')
  assert.equal(state.callbackStates[0], params.get('state'))
  assert.equal(state.callbackTimeouts[0], 5 * 60_000)
  assert.equal(state.closed, 1)
})

test('authorize: code 换 token 请求体带 code_verifier,且 sha256(verifier) 与授权页 challenge 一致', async () => {
  const { flow, state } = setup()
  await flow.authorize()

  assert.equal(state.calls.length, 1)
  const [call] = state.calls
  assert.equal(call.url, 'https://auth.example.com/token')
  assert.equal(call.init?.method, 'POST')
  const body = bodyParams(call)
  assert.equal(body.get('grant_type'), 'authorization_code')
  assert.equal(body.get('code'), 'auth-code-1')
  assert.equal(body.get('redirect_uri'), 'http://127.0.0.1:8090/api/auth/oidc/callback')
  assert.equal(body.get('client_id'), 'dashboard-gateway')
  const verifier = body.get('code_verifier') ?? ''
  assert.match(verifier, /^[A-Za-z0-9_-]{43}$/)
  assert.equal(createHash('sha256').update(verifier).digest('base64url'), authorizeParams(state).get('code_challenge'))
})

test('公共客户端: 未配置 secret 时 code 换 token / refresh 请求体均不含 client_secret', async () => {
  const { flow, state } = setup()
  await flow.authorize()
  const tokens = await flow.refresh('refresh-token-1')

  assert.equal(tokens.access_token, 'access-token-1')
  assert.equal(state.calls.length, 2)
  for (const call of state.calls) {
    assert.equal(bodyParams(call).has('client_secret'), false)
  }
  assert.equal(bodyParams(state.calls[0]).get('grant_type'), 'authorization_code')
  const refreshBody = bodyParams(state.calls[1])
  assert.equal(refreshBody.get('grant_type'), 'refresh_token')
  assert.equal(refreshBody.get('refresh_token'), 'refresh-token-1')
  assert.equal(refreshBody.get('client_id'), 'dashboard-gateway')
})

test('机密客户端兼容: 配置了 secret 时 code 换 token / refresh 仍携带 client_secret', async () => {
  const { flow, state } = setup({ clientSecret: 'oidc-secret' })
  await flow.authorize()
  await flow.refresh('refresh-token-1')

  assert.equal(bodyParams(state.calls[0]).get('client_secret'), 'oidc-secret')
  assert.equal(bodyParams(state.calls[1]).get('client_secret'), 'oidc-secret')
})

test('authorize: 交错启动两次,verifier 只与本次 URL 的 challenge 匹配(不复用共享值)', async () => {
  const { flow, state } = setup({ deferred: true })
  const first = flow.authorize()
  const second = flow.authorize()
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(state.opened.length, 2)
  assert.equal(state.pendingCodes.length, 2)

  state.pendingCodes[0]('code-1')
  state.pendingCodes[1]('code-2')
  await Promise.all([first, second])

  const bodyFor = (code: string): URLSearchParams => {
    const call = state.calls.find((c) => bodyParams(c).get('code') === code)
    assert.ok(call, `缺少 code=${code} 的 token 请求`)
    return bodyParams(call)
  }
  const v1 = bodyFor('code-1').get('code_verifier') ?? ''
  const v2 = bodyFor('code-2').get('code_verifier') ?? ''
  assert.match(v1, /^[A-Za-z0-9_-]{43}$/)
  assert.match(v2, /^[A-Za-z0-9_-]{43}$/)
  assert.notEqual(v1, v2)
  assert.equal(createHash('sha256').update(v1).digest('base64url'), new URL(state.opened[0]).searchParams.get('code_challenge'))
  assert.equal(createHash('sha256').update(v2).digest('base64url'), new URL(state.opened[1]).searchParams.get('code_challenge'))
})

test('authorize: openExternal 失败时回环仍被关闭', async () => {
  const { flow, state } = setup({ openExternalFails: true })
  await assert.rejects(() => flow.authorize(), /openExternal 失败/)
  assert.equal(state.closed, 1)
  assert.equal(state.calls.length, 0)
})

test('authorize: 换 token 失败抛出可读错误且关闭回环回调', async () => {
  const { flow, state } = setup()
  state.handler = async () => jsonResponse({}, 400)
  await assert.rejects(() => flow.authorize(), /SSO token 交换失败\(HTTP 400\)/)
  assert.equal(state.closed, 1)
})

test('refresh: 失败抛出「刷新失败(HTTP xxx)」', async () => {
  const { flow, state } = setup()
  state.handler = async () => jsonResponse({}, 500)
  await assert.rejects(() => flow.refresh('rt-1'), /刷新失败\(HTTP 500\)/)
})
