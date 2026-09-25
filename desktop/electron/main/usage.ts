import type { UsageBucket, UsageModelRow, UsageSummary } from '../../src/api/types'
import { readableAuthError, readErrorDetail } from './auth-errors'

function num(v: unknown): number {
  const n = typeof v === 'string' ? Number(v) : (v as number)
  return Number.isFinite(n) ? n : 0
}

function bucketOf(raw: unknown): UsageBucket {
  const b = (raw ?? {}) as Record<string, unknown>
  const tokensIn = num(b.tokensIn)
  const tokensOut = num(b.tokensOut)
  return {
    tokensIn,
    tokensOut,
    tokens: b.tokens === undefined ? tokensIn + tokensOut : num(b.tokens),
    cost: num(b.cost)
  }
}

function modelOf(raw: unknown): UsageModelRow {
  const m = (raw ?? {}) as Record<string, unknown>
  return {
    name: typeof m.name === 'string' ? m.name : '',
    today: bucketOf(m.today),
    month: bucketOf(m.month),
    dailyQuota: num(m.dailyQuota),
    monthlyQuota: num(m.monthlyQuota)
  }
}

/**
 * 把 GET /api/me/usage 的响应映射为 UsageSummary(纯函数,便于单测)。
 * 服务端响应已与 UsageSummary 同构(含 fetchedAt),这里仅做容错数值化与缺省兜底。
 */
export function parseUsageSummary(raw: unknown): UsageSummary {
  const src = (raw ?? {}) as Record<string, unknown>
  const quota = (src.quota ?? {}) as Record<string, unknown>
  const models = Array.isArray(src.models) ? src.models.map(modelOf) : []
  return {
    balance: num(src.balance),
    rateLimit: num(src.rateLimit),
    quota: { daily: num(quota.daily), monthly: num(quota.monthly) },
    today: bucketOf(src.today),
    month: bucketOf(src.month),
    models,
    truncated: src.truncated === true,
    fetchedAt: typeof src.fetchedAt === 'string' && src.fetchedAt ? src.fetchedAt : new Date().toISOString()
  }
}

export interface UsageFetchContext {
  /** router admin 根地址(如 https://ai.xzrobot.com/router) */
  baseUrl: string
  /** 取 router Bearer token(内部含刷新与单飞) */
  token: () => Promise<string>
  /** 401 自愈:清零 token 过期时间并强制重换(由身份层实现);缺省则不做重试 */
  refreshToken?: () => Promise<string>
  fetchImpl?: typeof fetch
}

/** 单请求取用量:GET {baseUrl}/api/me/usage + Bearer router token;401 重换 token 重试一次 */
export async function fetchUsageSummary(ctx: UsageFetchContext): Promise<UsageSummary> {
  const base = ctx.baseUrl.trim().replace(/\/+$/, '')
  if (!base) throw new Error('未配置路由管理端地址')
  const fetchImpl = ctx.fetchImpl ?? fetch
  const url = `${base}/api/me/usage`
  const request = (token: string): Promise<Response> =>
    fetchImpl(url, { headers: { Authorization: `Bearer ${token}` } })

  let res = await request(await ctx.token())
  if (res.status === 401 && ctx.refreshToken) {
    res = await request(await ctx.refreshToken())
  }
  if (!res.ok) {
    const authErr = readableAuthError(res.status, await readErrorDetail(res))
    if (authErr) throw authErr
    throw Object.assign(new Error(`网关管理端返回 HTTP ${res.status}`), { status: res.status })
  }
  return parseUsageSummary(await res.json())
}

/** 组装真实配置与凭证,供 IPC 调用 */
export async function getUsageSummary(): Promise<UsageSummary> {
  // store/identity 依赖 electron app,顶层静态导入会在纯 node 测试环境加载即抛错;
  // 故仅在此处动态导入,保持 fetchUsageSummary/parseUsageSummary 可独立单测。
  const [{ getConfig }, { freshRouterToken, renewRouterToken }] = await Promise.all([
    import('./store'),
    import('./identity')
  ])
  const cfg = getConfig()
  return fetchUsageSummary({
    baseUrl: cfg.routerAdminUrl ?? '',
    token: freshRouterToken,
    refreshToken: renewRouterToken
  })
}
