/**
 * router 凭据逻辑(token 交换 + apikey 自取):依赖全部注入,不依赖 Electron,便于单测。
 * identity.ts 负责把 store/oidc 的真实实现接进来。
 */

import { readableAuthError, readErrorDetail } from './auth-errors'

export interface RouterCredentialIdentity {
  user: { id: string | number }
  oidc: { idToken: string } | null
  /** router apikey(sk-),供 gateway 调用与本地 agent 使用 */
  routerKey: string
  /** router access_token,仅供 router admin /api/me/* 的 Bearer 使用 */
  routerToken?: string
  /** routerToken 过期时间戳(ms) */
  routerTokenExpiresAt?: number
}

export interface RouterCredentialDeps<T extends RouterCredentialIdentity> {
  getIdentity: () => T | null
  saveIdentity: (identity: T) => void
  /** 确保 oidc.idToken 新鲜(exchange 前调用) */
  ensureFreshOidc: () => Promise<void>
  issuer: () => string
  oidcClient: () => { clientId: string; clientSecret: string }
  routerAdminUrl: () => string
  fetchImpl?: typeof fetch
  now?: () => number
  /** token 余量:剩余有效期小于该值时视为需要交换(默认 60s) */
  refreshMarginMs?: number
}

export interface RouterCredentials {
  freshRouterToken: () => Promise<string>
  /** 401 自愈:清零过期时间落盘后强制重换 */
  renewRouterToken: () => Promise<string>
  fetchRouterKey: () => Promise<string>
}

const TOKEN_EXCHANGE_GRANT = 'urn:ietf:params:oauth:grant-type:token-exchange'
const ID_TOKEN_TYPE = 'urn:ietf:params:oauth:token-type:id_token'

export function createRouterCredentials<T extends RouterCredentialIdentity>(
  deps: RouterCredentialDeps<T>
): RouterCredentials {
  const fetchImpl = deps.fetchImpl ?? fetch
  const now = deps.now ?? ((): number => Date.now())
  const margin = deps.refreshMarginMs ?? 60_000
  /** 单飞仅在同一身份内生效;换人后旧 inflight 被丢弃,避免跨用户串用凭据 */
  let inflight: { sub: string; promise: Promise<string> } | null = null

  /** 用当前 id_token 走 RFC 8693 token-exchange;成功写回 routerToken 并持久化 */
  async function exchangeRouterToken(): Promise<string> {
    const identity = deps.getIdentity()
    if (!identity) throw new Error('请先完成企业 SSO 登录')
    const sub = String(identity.user.id)
    try {
      await deps.ensureFreshOidc()
    } catch (err) {
      throw new Error(`企业登录状态已失效,请重新登录企业账号(${(err as Error).message})`)
    }
    const fresh = deps.getIdentity()
    const idToken = fresh?.oidc?.idToken
    if (!idToken) throw new Error('当前身份无 SSO 凭据,请退出后重新 SSO 登录')

    const { clientId, clientSecret } = deps.oidcClient()
    const params: Record<string, string> = {
      grant_type: TOKEN_EXCHANGE_GRANT,
      subject_token: idToken,
      subject_token_type: ID_TOKEN_TYPE,
      audience: 'router',
      client_id: clientId
    }
    // 公共客户端(未配置 secret)不带 client_secret;机密部署仍携带
    if (clientSecret) params.client_secret = clientSecret
    let res: Response
    try {
      res = await fetchImpl(`${deps.issuer()}/token`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams(params)
      })
    } catch (err) {
      throw new Error(`router token 交换失败(网络异常):${(err as Error).message}`)
    }
    if (!res.ok) throw new Error(`router token 交换失败(HTTP ${res.status})`)
    const data = (await res.json()) as { access_token?: string; expires_in?: number }
    if (!data.access_token) throw new Error('router token 交换响应缺少 access_token')

    // 交换期间可能已重新登录或登出:身份代际不一致时丢弃结果,绝不写回/返回旧用户 token
    const target = deps.getIdentity()
    if (!target || String(target.user.id) !== sub) throw new Error('身份已变更,取消写入')
    target.routerToken = data.access_token
    target.routerTokenExpiresAt = now() + (data.expires_in ?? 3600) * 1000
    deps.saveIdentity(target)
    return data.access_token
  }

  /** 取新鲜 router token:未过期直接返回;过期/缺失时交换;同身份并发单飞;交换失败回退未过期的旧 token */
  function freshRouterToken(): Promise<string> {
    const identity = deps.getIdentity()
    if (!identity) return Promise.reject(new Error('请先完成企业 SSO 登录'))
    const sub = String(identity.user.id)
    const token = identity.routerToken
    if (token && (identity.routerTokenExpiresAt ?? 0) > now() + margin) return Promise.resolve(token)
    if (inflight && inflight.sub === sub) return inflight.promise
    let entry: { sub: string; promise: Promise<string> } | null = null
    const promise = (async () => {
      try {
        return await exchangeRouterToken()
      } catch (err) {
        const fallback = deps.getIdentity()
        if (
          fallback &&
          String(fallback.user.id) === sub &&
          fallback.routerToken &&
          (fallback.routerTokenExpiresAt ?? 0) > now()
        ) {
          return fallback.routerToken
        }
        throw err
      } finally {
        if (entry && inflight === entry) inflight = null
      }
    })()
    entry = { sub, promise }
    inflight = entry
    return promise
  }

  /** 401 自愈:清零过期时间落盘后强制重换 token(同身份内单飞;身份缺失则要求重登) */
  function renewRouterToken(): Promise<string> {
    const identity = deps.getIdentity()
    if (!identity) return Promise.reject(new Error('请先完成企业 SSO 登录'))
    identity.routerTokenExpiresAt = 0
    deps.saveIdentity(identity)
    return freshRouterToken()
  }

  async function requestKey(base: string): Promise<Response> {
    const token = await freshRouterToken()
    try {
      return await fetchImpl(`${base}/api/me/key`, {
        headers: { Authorization: `Bearer ${token}` }
      })
    } catch (err) {
      throw new Error(`router key 获取失败(网络异常):${(err as Error).message}`)
    }
  }

  /** 取 router apikey(sk-):已有直接返回;否则调 /api/me/key,401 时置 0 重换 token 并重试一次;全程绑定发起时身份 */
  async function fetchRouterKey(): Promise<string> {
    const identity = deps.getIdentity()
    if (!identity) throw new Error('请先完成企业 SSO 登录')
    const sub = String(identity.user.id)
    if (identity.routerKey) return identity.routerKey

    // 每个有副作用的节点前校验身份代际,与 exchangeRouterToken 的写回校验对称
    const sameIdentity = (): T => {
      const target = deps.getIdentity()
      if (!target || String(target.user.id) !== sub) throw new Error('身份已变更,取消写入')
      return target
    }

    const base = deps.routerAdminUrl().trim().replace(/\/+$/, '')
    if (!base) throw new Error('未配置路由管理端地址')
    let res = await requestKey(base)
    if (res.status === 401) {
      const retry = sameIdentity()
      retry.routerTokenExpiresAt = 0
      deps.saveIdentity(retry)
      sameIdentity() // 重换 token 与重试请求前再确认仍是同一身份
      res = await requestKey(base)
    }
    if (!res.ok) {
      const authErr = readableAuthError(res.status, await readErrorDetail(res))
      throw authErr ?? new Error(`router key 获取失败(HTTP ${res.status})`)
    }
    const data = (await res.json()) as { key?: string }
    if (!data.key) throw new Error('router key 响应缺少 key')

    // 往返期间可能已重新登录:身份代际不一致时丢弃结果,绝不写回/返回旧用户 sk-
    const target = sameIdentity()
    target.routerKey = data.key
    deps.saveIdentity(target)
    return data.key
  }

  return { freshRouterToken, renewRouterToken, fetchRouterKey }
}
