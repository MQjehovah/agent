/**
 * 能力市场直连客户端(可注入工厂)：
 *   my/capabilities 订阅列表、订阅/取消订阅、按名称下载能力包。
 *   与 rag.ts 的 createRagSearcher 同构 —— URL 拼接、OIDC Bearer 注入、错误折叠
 *   收敛到这里；fetch 实现可注入便于单测。客户端网络错误统一抛中文 Error，
 *   由上层(ipc)统一处理，不在此折叠成 ok:false。
 */

/** 非 2xx 正文里按优先级尝试取文案的字段 */
const ERROR_FIELDS = ['detail', 'error', 'message']
/** 拼接进错误文案的正文截断上限(防止把整段堆栈塞进提示) */
const RAW_BODY_LIMIT = 200

/** dashboard 本地模式支持安装的四类能力(workflow 等由市场侧消费,不进列表) */
export type MarketCapabilityType = 'agent' | 'tool' | 'skill' | 'mcp'

/** 能力运行规格(market §3;dashboard 只消费 cloud/local,其余字段按需再扩展) */
export interface MarketRuntime {
  cloud?: boolean
  local?: boolean
  recommended?: 'cloud' | 'local'
}

/** my/capabilities 响应的最小映射字段(容错取值,多余字段忽略) */
export interface MarketCapability {
  id: string
  name: string
  type: MarketCapabilityType
  version: string
  description?: string
  status?: string
  author_name?: string
  added?: boolean
  owned?: boolean
  /** 分发方式 local|remote|both(容错取字符串;缺失/非法时不带,由调用方按 both 处理) */
  distribution?: string
  /** 运行规格(优先于 distribution;缺失回退) */
  runtime?: MarketRuntime
}

export interface MarketClientDeps {
  /** 市场服务地址(可带尾斜杠,内部去掉) */
  marketUrl: string
  /** 取 OIDC access token;返回 null 表示尚未完成企业 SSO 登录 */
  getToken: () => string | null
  /** 可注入的 fetch 实现,测试用 stub 替换;缺省用全局 fetch */
  fetchImpl?: typeof fetch
}

export interface MarketClient {
  /** 我的能力列表(scope=added|owned|all,默认 added:从市场加入的) */
  listMy(scope?: string): Promise<MarketCapability[]>
  /** 加入我的能力(订阅);body 为 capability_id */
  subscribe(capabilityId: string): Promise<void>
  /** 从我的能力移除(取消订阅) */
  unsubscribe(capabilityId: string): Promise<void>
  /** 按名称下载能力包(可带版本),返回原始 bytes */
  download(name: string, version?: string): Promise<Buffer>
  /** 取能力图标(返回 data URL;无图标或失败返回空串) */
  icon(id: string): Promise<string>
}

/** 从非 2xx 正文里提炼短原因:优先 JSON 的 detail/error/message,否则截断原文 */
function bodyReason(raw: string): string {
  const text = raw.trim()
  if (!text) return ''
  try {
    const parsed = JSON.parse(text) as Record<string, unknown>
    for (const key of ERROR_FIELDS) {
      const v = parsed?.[key]
      if (typeof v === 'string' && v.trim()) return v.trim()
    }
  } catch {
    // 非 JSON 正文,落到下方截断原文
  }
  return text.slice(0, RAW_BODY_LIMIT)
}

/** 网络层异常折叠:保留原 message,有 cause 时拼上 cause 文案便于定位 */
function toNetworkError(err: unknown): Error {
  const e = err instanceof Error ? err : new Error(String(err))
  const cause = (e as { cause?: unknown }).cause
  if (cause == null) return new Error(`市场请求失败: ${e.message}`)
  const causeText = cause instanceof Error ? cause.message : String(cause)
  return new Error(`市场请求失败: ${e.message}(cause: ${causeText})`)
}

export function createMarketClient(deps: MarketClientDeps): MarketClient {
  const doFetch = deps.fetchImpl ?? fetch
  const base = deps.marketUrl.replace(/\/+$/, '')

  /**
   * 统一请求入口:取 token(缺失即抛 SSO 提示)→ 拼接 URL → 注入 Bearer →
   * 网络错误折叠(cause)→ 非 2xx 取 detail/error/message 后抛错。返回已 2xx 的 Response。
   */
  async function request(method: 'GET' | 'POST' | 'DELETE', path: string, body?: unknown): Promise<Response> {
    const token = deps.getToken()
    if (!token) throw new Error('请先完成企业 SSO 登录(访问能力市场需要)')
    const url = `${base}${path.startsWith('/') ? path : `/${path}`}`
    const headers: Record<string, string> = {
      authorization: `Bearer ${token}`,
      'content-type': 'application/json'
    }
    const init: RequestInit = { method, headers }
    if (body !== undefined) init.body = JSON.stringify(body)
    let res: Response
    try {
      res = await doFetch(url, init)
    } catch (err) {
      throw toNetworkError(err)
    }
    if (!res.ok) {
      const raw = await res.text().catch(() => '')
      const reason = bodyReason(raw)
      throw new Error(`市场请求失败(HTTP ${res.status})${reason ? `：${reason}` : ''}`)
    }
    return res
  }

  /** runtime 容错映射:只收消费字段(cloud/local/recommended);非法/空对象返回 null */
  function toRuntime(v: unknown): MarketRuntime | null {
    if (typeof v !== 'object' || v === null || Array.isArray(v)) return null
    const r = v as Record<string, unknown>
    const out: MarketRuntime = {}
    if (typeof r.cloud === 'boolean') out.cloud = r.cloud
    if (typeof r.local === 'boolean') out.local = r.local
    if (r.recommended === 'cloud' || r.recommended === 'local') out.recommended = r.recommended
    return Object.keys(out).length ? out : null
  }

  /** 把 my/capabilities 单个条目容错映射为 MarketCapability;不支持类型/缺关键字段返回 null */
  function toCapability(v: unknown): MarketCapability | null {
    if (typeof v !== 'object' || v === null || Array.isArray(v)) return null
    const r = v as Record<string, unknown>
    const id = typeof r.id === 'string' && r.id ? r.id : null
    const name = typeof r.name === 'string' && r.name ? r.name : null
    const version = typeof r.version === 'string' && r.version ? r.version : null
    if (!id || !name || !version) return null
    const type =
      r.type === 'agent' || r.type === 'tool' || r.type === 'skill' || r.type === 'mcp' ? r.type : null
    if (!type) return null // workflow 等非本地四类不进入列表
    const out: MarketCapability = { id, name, type, version }
    const desc = typeof r.description === 'string' ? r.description : null
    if (desc !== null) out.description = desc
    const status = typeof r.status === 'string' ? r.status : null
    if (status !== null) out.status = status
    const author = typeof r.author_name === 'string' ? r.author_name : null
    if (author !== null) out.author_name = author
    const distribution = typeof r.distribution === 'string' && r.distribution ? r.distribution : null
    if (distribution !== null) out.distribution = distribution
    const runtime = toRuntime(r.runtime)
    if (runtime !== null) out.runtime = runtime
    if (typeof r.added === 'boolean') out.added = r.added
    if (typeof r.owned === 'boolean') out.owned = r.owned
    return out
  }

  return {
    async listMy(scope = 'added') {
      const res = await request('GET', `/api/my/capabilities?scope=${encodeURIComponent(scope)}`)
      const parsed = (await res.json()) as unknown
      const items = Array.isArray(parsed) ? parsed : []
      return items
        .map(toCapability)
        .filter((c): c is MarketCapability => c !== null)
    },

    async subscribe(capabilityId) {
      await request('POST', '/api/my/capabilities', { capability_id: capabilityId })
    },

    async unsubscribe(capabilityId) {
      await request('DELETE', `/api/my/capabilities/${encodeURIComponent(capabilityId)}`)
    },

    async download(name, version) {
      const suffix = version ? `?version=${encodeURIComponent(version)}` : ''
      const res = await request('GET', `/api/capabilities/${encodeURIComponent(name)}/download${suffix}`)
      return Buffer.from(await res.arrayBuffer())
    },

    async icon(id) {
      try {
        const res = await request('GET', `/api/capabilities/${encodeURIComponent(id)}/icon`)
        const buf = Buffer.from(await res.arrayBuffer())
        const ct = res.headers.get('content-type') || 'image/png'
        return `data:${ct};base64,${buf.toString('base64')}`
      } catch {
        return ''
      }
    }
  }
}
