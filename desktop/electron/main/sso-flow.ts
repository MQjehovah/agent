/**
 * OIDC 授权码 + PKCE 流程(依赖全部注入,不依赖 Electron,便于单测)。
 * identity.ts 负责把系统浏览器、回环回调与 store 的真实实现接进来。
 * 公共客户端:code_verifier 仅存内存;client_secret 可选,非空时才随请求携带(兼容机密部署)。
 */

import { createHash, randomBytes } from 'node:crypto'

export interface SsoTokenResponse {
  id_token?: string
  access_token?: string
  refresh_token?: string
  expires_in?: number
}

export interface SsoFlowDeps {
  issuer: () => string
  /** client_secret 允许为空串:公共客户端仅凭 client_id + PKCE 认证 */
  oidcClient: () => { clientId: string; clientSecret: string }
  redirectUri: () => string
  /** 用系统浏览器打开授权页 */
  openExternal: (url: string) => Promise<void>
  /** 打开浏览器前注册回环回调:返回等待授权码的 Promise 与关闭函数 */
  beginCallback: (state: string, timeoutMs: number) => Promise<{ code: Promise<string>; close: () => void }>
  fetchImpl?: typeof fetch
}

export interface SsoFlow {
  /** 授权码 + PKCE 登录:生成 verifier/challenge → 打开授权页 → 等回调 → code+verifier 换 token */
  authorize: (timeoutMs?: number) => Promise<SsoTokenResponse>
  /** refresh_token 续期 */
  refresh: (refreshToken: string) => Promise<SsoTokenResponse>
}

/** verifier = 32 字节随机数的 base64url(43 字符);challenge = base64url(sha256(verifier)),method S256 */
export function createPkcePair(): { codeVerifier: string; codeChallenge: string; codeChallengeMethod: 'S256' } {
  const codeVerifier = randomBytes(32).toString('base64url')
  const codeChallenge = createHash('sha256').update(codeVerifier).digest('base64url')
  return { codeVerifier, codeChallenge, codeChallengeMethod: 'S256' }
}

/** 组装 token 请求体:仅在配置了非空 client_secret 时才带 client_secret */
function tokenParams(fields: Record<string, string>, clientSecret: string): URLSearchParams {
  const params = new URLSearchParams(fields)
  if (clientSecret) params.set('client_secret', clientSecret)
  return params
}

export function createSsoFlow(deps: SsoFlowDeps): SsoFlow {
  const fetchImpl = deps.fetchImpl ?? fetch

  async function postToken(body: URLSearchParams, failPrefix: string): Promise<SsoTokenResponse> {
    const res = await fetchImpl(`${deps.issuer()}/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body
    })
    if (!res.ok) throw new Error(`${failPrefix}(HTTP ${res.status})`)
    return (await res.json()) as SsoTokenResponse
  }

  async function authorize(timeoutMs = 5 * 60_000): Promise<SsoTokenResponse> {
    const { clientId, clientSecret } = deps.oidcClient()
    const redirectUri = deps.redirectUri()
    const pkce = createPkcePair()
    const state = randomBytes(16).toString('hex')
    const callback = await deps.beginCallback(state, timeoutMs)
    try {
      const params = new URLSearchParams({
        response_type: 'code',
        client_id: clientId,
        redirect_uri: redirectUri,
        state,
        scope: 'openid profile',
        code_challenge: pkce.codeChallenge,
        code_challenge_method: pkce.codeChallengeMethod
      })
      await deps.openExternal(`${deps.issuer()}/authorize?${params.toString()}`)
      const code = await callback.code
      return await postToken(
        tokenParams(
          {
            grant_type: 'authorization_code',
            code,
            redirect_uri: redirectUri,
            client_id: clientId,
            code_verifier: pkce.codeVerifier
          },
          clientSecret
        ),
        'SSO token 交换失败'
      )
    } finally {
      callback.close()
    }
  }

  async function refresh(refreshToken: string): Promise<SsoTokenResponse> {
    const { clientId, clientSecret } = deps.oidcClient()
    return await postToken(
      tokenParams({ grant_type: 'refresh_token', refresh_token: refreshToken, client_id: clientId }, clientSecret),
      '刷新失败'
    )
  }

  return { authorize, refresh }
}
