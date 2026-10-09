/**
 * OIDC 刷新协调(依赖全部注入,不依赖 Electron,便于单测):
 * SSO 的 refresh_token 单次使用轮换,并发刷新会让后到的一发被 SSO 判为无效(invalid_grant 400)。
 * 本模块把同进程所有刷新入口合并为一次网络请求(单飞),失败进入冷却窗口防重试风暴,
 * 并用「tokens 对象代际」守卫,防止刷新结果覆盖期间发生的重新登录。
 */

export interface OidcTokensLike {
  idToken: string
  accessToken: string
  refreshToken: string
  /** access_token 过期时间戳(ms) */
  expiresAt: number
}

export interface OidcRefreshResponse {
  id_token?: string
  access_token?: string
  refresh_token?: string
  expires_in?: number
}

export interface OidcRefresherDeps {
  /** 当前 OIDC tokens(每次刷新成功/重新登录都会替换为新对象,对象本身即身份代际) */
  getTokens: () => OidcTokensLike | null
  /** 写回新 tokens;expected 已不是当前代际(期间重新登录/登出)时返回 false 丢弃,绝不覆盖新会话 */
  applyTokens: (tokens: OidcTokensLike, expected: OidcTokensLike) => boolean
  /** 发起 SSO refresh_token 刷新(如 ssoFlow.refresh) */
  refresh: (refreshToken: string) => Promise<OidcRefreshResponse>
  /** 刷新成功且已应用后的副作用(如复投 agent 托管);仅在真正应用时触发 */
  afterRefresh?: () => Promise<void> | void
  now?: () => number
  /** 提前刷新余量:剩余有效期小于该值时视为需要刷新(默认 60s) */
  freshMarginMs?: number
  /** 失败冷却(默认 60s):冷却期内直接抛上次错误,跳过重复网络请求 */
  failCooldownMs?: number
}

export interface OidcRefresher {
  /** 确保 tokens 新鲜:未临近过期直接返回;临近过期时刷新(同进程并发合并为一次) */
  ensureFresh: () => Promise<void>
  /** 重置失败冷却(SSO 登录成功/清空身份时调用) */
  resetFailure: () => void
}

export function createOidcRefresher(deps: OidcRefresherDeps): OidcRefresher {
  const now = deps.now ?? ((): number => Date.now())
  const freshMargin = deps.freshMarginMs ?? 60_000
  const failCooldown = deps.failCooldownMs ?? 60_000

  let failedAt = 0
  let failedError: Error | null = null
  let inflight: Promise<void> | null = null

  async function doRefresh(expected: OidcTokensLike, refreshToken: string): Promise<void> {
    const data = await deps.refresh(refreshToken)
    if (!data.access_token) throw new Error('刷新响应缺少 access_token')
    // 网络层已成功:无论结果是否被采用,都视为刷新成功,清掉失败冷却
    failedAt = 0
    failedError = null

    const next: OidcTokensLike = {
      idToken: data.id_token ?? expected.idToken,
      accessToken: data.access_token,
      refreshToken: data.refresh_token ?? refreshToken,
      expiresAt: now() + (data.expires_in ?? 3600) * 1000
    }
    // 刷新期间可能已重新登录/登出(代际变化):丢弃结果,不触发副作用
    if (!deps.applyTokens(next, expected)) return
    await deps.afterRefresh?.()
  }

  function ensureFresh(): Promise<void> {
    const tokens = deps.getTokens()
    if (!tokens) return Promise.resolve()
    if (tokens.expiresAt > now() + freshMargin) return Promise.resolve()
    if (!tokens.refreshToken) return Promise.reject(new Error('无 refresh_token'))
    // 冷却期内直接抛同错:防重复网络请求与重试风暴
    if (failedAt && now() - failedAt < failCooldown) {
      return Promise.reject(failedError ?? new Error('OIDC 刷新失败(冷却中)'))
    }
    if (inflight) return inflight

    let entry: Promise<void> | null = null
    const promise = doRefresh(tokens, tokens.refreshToken)
      .catch((err: unknown) => {
        failedAt = now()
        failedError = err instanceof Error ? err : new Error(String(err))
        throw failedError
      })
      .finally(() => {
        if (inflight === entry) inflight = null
      })
    entry = promise
    inflight = entry
    return promise
  }

  return {
    ensureFresh,
    resetFailure(): void {
      failedAt = 0
      failedError = null
    }
  }
}
