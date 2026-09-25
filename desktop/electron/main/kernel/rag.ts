/**
 * RAG 直连调用(可注入工厂)：
 *   kb_search 等工具要直连公司知识库的 /api/search，须带 OIDC access token
 *   (语义同 upstream.ts 的 authFor('rag'))。把 URL 拼接、鉴权、错误折叠收敛到这里，
 *   fetch 实现可注入便于单测；错误一律折叠为带上下文的 Error。
 */

/** 非 2xx 正文里按优先级尝试取文案的字段 */
const ERROR_FIELDS = ['detail', 'error', 'message']
/** 拼接进错误文案的正文截断上限(防止把整段堆栈塞进提示) */
const RAW_BODY_LIMIT = 200

export interface RagSearcherDeps {
  /** RAG 服务地址(可带尾斜杠,内部去掉) */
  ragUrl: string
  /** 取 OIDC access token;返回 null 表示尚未完成企业 SSO 登录 */
  getToken: () => string | null
  /** 可注入的 fetch 实现,测试用 stub 替换;缺省用全局 fetch */
  fetchImpl?: typeof fetch
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
  const cause = e.cause
  if (cause == null) return new Error(`知识库检索失败: ${e.message}`)
  const causeText = cause instanceof Error ? cause.message : String(cause)
  return new Error(`知识库检索失败: ${e.message}(cause: ${causeText})`)
}

/** 构造 RAG 检索器:入参注入地址/token/fetch,返回 (path, body) => JSON(已 parse) */
export function createRagSearcher(deps: RagSearcherDeps): (path: string, body: unknown) => Promise<unknown> {
  const doFetch = deps.fetchImpl ?? fetch
  return async (path, body) => {
    const token = deps.getToken()
    if (!token) throw new Error('请先完成企业 SSO 登录(知识库检索需要)')
    const base = deps.ragUrl.replace(/\/+$/, '')
    const url = `${base}${path.startsWith('/') ? path : `/${path}`}`
    let res: Response
    try {
      res = await doFetch(url, {
        method: 'POST',
        headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
        body: JSON.stringify(body)
      })
    } catch (err) {
      throw toNetworkError(err)
    }
    if (!res.ok) {
      const raw = await res.text().catch(() => '')
      const reason = bodyReason(raw)
      throw new Error(`知识库检索失败(HTTP ${res.status})${reason ? `：${reason}` : ''}`)
    }
    return (await res.json()) as unknown
  }
}
