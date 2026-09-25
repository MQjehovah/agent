import type { ToolDefinition, ToolResult } from './types'

/**
 * 市场远程 tool 适配：把 market 上 type=tool 的能力包装为内核工具 market:<name>，
 * 真实调用委托给注入的 invoke（ipc 注入云端 runtime 调用，本模块保持纯逻辑可离线单测）。
 * 与 mcp.ts 的 wrapMcpTool 同构：execute 不抛异常，成功折叠为文本、异常/远程失败折叠 ok:false。
 */

/** LLM 侧单参包裹：工具的全部入参放在 params 对象里（服务器 RuntimeInvokeRequest 同形） */
const PARAMS_SCHEMA = {
  type: 'object',
  properties: {
    params: { type: 'object', description: '市场工具入参对象，具体键由该工具定义' }
  },
  required: ['params']
}

export interface MarketToolCapMeta {
  name: string
  description?: string
}

/** 远程调用委托：reject 视为远程调用失败，错误文案会原样出现在 ok:false 的输出里 */
export type MarketToolInvoke = (name: string, params: unknown) => Promise<unknown>

/** 任意返回值折叠为文本：字符串/基础类型原样，对象 JSON 序列化；序列化失败退 String 兜底 */
function serializeResult(v: unknown): string {
  if (v === undefined) return '(无输出)'
  if (v === null) return 'null'
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  try {
    const s = JSON.stringify(v)
    if (s !== undefined) return s
  } catch {
    // 循环引用等不可序列化对象，落到 String 兜底
  }
  return String(v)
}

/** 纯工厂：入参注入 cap 元信息与 invoke，产出可直接注册进 registry 的远程 tool 定义 */
export function createMarketTool(cap: MarketToolCapMeta, invoke: MarketToolInvoke): ToolDefinition {
  const description = cap.description?.trim()
  const hint = '调用时把该工具的入参整体放入 params 对象'
  return {
    name: `market:${cap.name}`,
    description: description ? `${description}\n${hint}。` : `调用市场远程工具 ${cap.name}：${hint}。`,
    kind: 'read',
    parameters: PARAMS_SCHEMA,
    async execute(args): Promise<ToolResult> {
      try {
        const params = args?.params ?? {}
        return { ok: true, output: serializeResult(await invoke(cap.name, params)) }
      } catch (e) {
        return { ok: false, output: e instanceof Error ? e.message : String(e) }
      }
    }
  }
}

// ---- 真实云端调用（ipc 注入用） ----

/** 非 2xx 正文里按优先级尝试取文案的字段（与 market.ts/rag.ts 同构） */
const ERROR_FIELDS = ['detail', 'error', 'message']
/** 拼接进错误文案的正文截断上限 */
const RAW_BODY_LIMIT = 200

/** 远程调用失败正文提炼：优先 JSON 的 detail/error/message，否则截断原文 */
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
    // 非 JSON 正文，落到下方截断原文
  }
  return text.slice(0, RAW_BODY_LIMIT)
}

/** 网络层异常折叠：保留 cause 文案便于定位（与 market.ts/rag.ts 同构） */
function toNetworkError(err: unknown): Error {
  const e = err instanceof Error ? err : new Error(String(err))
  const cause = (e as { cause?: unknown }).cause
  if (cause == null) return new Error(`市场远程调用失败: ${e.message}`)
  const causeText = cause instanceof Error ? cause.message : String(cause)
  return new Error(`市场远程调用失败: ${e.message}(cause: ${causeText})`)
}

/** 运行结果里的 error 字段转可读文本（字符串/对象均可） */
function errorText(v: unknown): string {
  if (typeof v === 'string' && v.trim()) return v.trim()
  try {
    const s = JSON.stringify(v)
    if (s && s !== '{}') return s
  } catch {
    // 落到兜底
  }
  return '未知错误'
}

export interface MarketToolInvokerDeps {
  /** 市场服务地址(可带尾斜杠,内部去掉) */
  marketUrl: string
  /** 取 OIDC access token;返回 null 表示尚未完成企业 SSO 登录 */
  getToken: () => string | null
  /** 可注入的 fetch 实现,测试用 stub 替换;缺省用全局 fetch */
  fetchImpl?: typeof fetch
}

/**
 * 构造真实云端 invoke：POST {market}/api/runtime/tools/{name}/invoke，Bearer 注入。
 * HTTP 非 2xx 折叠为带 detail 的中文 Error；2xx 但 result.status≠ok（HTTP 仍 200 的执行失败）
 * 也折叠为带 error detail 的中文 Error——保证上层工厂统一以异常表示失败。
 * 成功返回 runtimes 响应的 result 对象（含 status/output/execution）。
 */
export function createMarketToolInvoker(deps: MarketToolInvokerDeps): MarketToolInvoke {
  const doFetch = deps.fetchImpl ?? fetch
  const base = deps.marketUrl.replace(/\/+$/, '')
  return async (name, params) => {
    const token = deps.getToken()
    if (!token) throw new Error('请先完成企业 SSO 登录(调用市场远程工具需要)')
    const url = `${base}/api/runtime/tools/${encodeURIComponent(name)}/invoke`
    let res: Response
    try {
      res = await doFetch(url, {
        method: 'POST',
        headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
        body: JSON.stringify({ params: params ?? {} })
      })
    } catch (err) {
      throw toNetworkError(err)
    }
    if (!res.ok) {
      const raw = await res.text().catch(() => '')
      const reason = bodyReason(raw)
      throw new Error(`市场远程调用失败(HTTP ${res.status})${reason ? `：${reason}` : ''}`)
    }
    const payload = (await res.json().catch(() => null)) as unknown
    const obj =
      typeof payload === 'object' && payload !== null ? (payload as Record<string, unknown>) : {}
    const result = obj.result
    if (typeof result === 'object' && result !== null) {
      const r = result as Record<string, unknown>
      if (typeof r.status === 'string' && r.status !== 'ok') {
        throw new Error(`市场远程工具执行失败：${errorText(r.error)}`)
      }
      return result
    }
    return payload
  }
}
