import type { ToolDefinition } from './types'

/**
 * 本地 agent 渐进披露（工具搜索）纯逻辑：
 * 连接器装多后不再每轮把全部远程工具定义发给模型，而是先发内置工具 + search_tools，
 * 由模型按需调用 search_tools 检索，命中后把远程工具「激活」进本会话（会话内粘住）。
 * 全部为纯函数/工厂，无 IO，便于离线单测；配置读取与会话持久化由 ipc 层注入。
 */

export type ToolSearchMode = 'auto' | 'always' | 'off'

/** auto 模式的默认阈值：已注册远程工具数 > 40 时启用渐进披露 */
export const TOOL_SEARCH_THRESHOLD = 40
/** search_tools 的 limit 缺省值与夹紧范围 */
export const SEARCH_TOOLS_DEFAULT_LIMIT = 8
export const SEARCH_TOOLS_MAX_LIMIT = 20

/** search_tools 注册名（渐进模式下由内置工具通道恒发） */
export const SEARCH_TOOLS_NAME = 'search_tools'

/**
 * 远程工具判定：连接器注册进 registry 的工具——
 * MCP 的 `mcp__<server>__<tool>`（mcp.ts mcpToolName）与市场远程 `market:<name>`（market-tools.ts）。
 * 禁用/卸载的连接器不会被注册，因此天然不计入。
 */
export function isRemoteTool(name: string): boolean {
  return name.startsWith('mcp__') || name.startsWith('market:')
}

/** 配置值规范化：存储里出现脏值时回退 auto，调用方无需处理非法入参 */
export function normalizeToolSearchMode(value: unknown): ToolSearchMode {
  return value === 'always' || value === 'off' ? value : 'auto'
}

/** 是否启用渐进披露：off 恒否；always 恒是；auto 仅当已注册远程工具数 > 阈值 */
export function shouldUseProgressive(
  mode: ToolSearchMode,
  remoteCount: number,
  threshold: number = TOOL_SEARCH_THRESHOLD
): boolean {
  if (mode === 'off') return false
  if (mode === 'always') return true
  return remoteCount > threshold
}

/** 检索条目：registry 里的工具名与描述（仅需这两项） */
export interface ToolSearchEntry {
  name: string
  description: string
}

/** 从工具名解析所属连接器：mcp__<server>__<tool> → server，market:<name> → market，无法判定回空串 */
export function connectorOf(name: string): string {
  if (name.startsWith('mcp__')) {
    const rest = name.slice('mcp__'.length)
    const at = rest.indexOf('__')
    return at > 0 ? rest.slice(0, at) : ''
  }
  if (name.startsWith('market:')) return 'market'
  return ''
}

/** 检索命中条目：score 用于排序与测试断言 */
export interface ToolSearchHit {
  name: string
  description: string
  score: number
}

/** limit 夹紧：非有限值/缺省取 8，结果恒在 1..20 */
export function clampLimit(limit?: number): number {
  const n = typeof limit === 'number' && Number.isFinite(limit)
    ? Math.floor(limit)
    : SEARCH_TOOLS_DEFAULT_LIMIT
  return Math.min(Math.max(n, 1), SEARCH_TOOLS_MAX_LIMIT)
}

/**
 * 远程工具检索（纯函数）：
 * query 按空白分词，每个词在「名称 / 连接器 / 描述」中做不区分大小写的子串匹配并计分
 * （名称命中权重最高、连接器其次、描述最低），至少命中一个词才入选；
 * 按分数降序、同分按名称升序稳定排序，截取 limit（夹紧 1..20，缺省 8）。
 * 空查询/空白查询返回空数组（由调用方提示补充关键词）。
 */
export function searchRemoteTools(
  entries: readonly ToolSearchEntry[],
  query: string,
  limit?: number
): ToolSearchHit[] {
  const terms = String(query ?? '')
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
  if (terms.length === 0) return []
  const hits: ToolSearchHit[] = []
  for (const entry of entries) {
    if (!entry || typeof entry.name !== 'string' || !entry.name) continue
    const name = entry.name.toLowerCase()
    const connector = connectorOf(entry.name).toLowerCase()
    const description = String(entry.description ?? '').toLowerCase()
    let score = 0
    for (const term of terms) {
      if (name === term) score += 100
      else if (name.includes(term)) score += 10
      if (connector && connector.includes(term)) score += 5
      if (description.includes(term)) score += 2
    }
    if (score > 0) hits.push({ name: entry.name, description: entry.description, score })
  }
  hits.sort((a, b) => b.score - a.score || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
  return hits.slice(0, clampLimit(limit))
}

/** search_tools 输出：逐条「名称 —— [连接器 x] 描述」，并说明命中工具已激活、下一轮可直接调用 */
export function formatSearchResult(hits: readonly ToolSearchHit[]): string {
  const lines = hits.map((hit) => {
    const connector = connectorOf(hit.name)
    const tag = connector ? `[连接器 ${connector}] ` : ''
    const desc = String(hit.description ?? '').replace(/\s+/g, ' ').trim()
    return `${hit.name} —— ${tag}${desc || '(无描述)'}`
  })
  return [`找到 ${hits.length} 个匹配的远程工具（已激活，下一轮对话可直接调用）：`, ...lines].join('\n')
}

/** 渐进模式下追加到系统提示末尾的说明：告知先搜索、并列出本会话已激活的工具 */
export function progressiveHint(activeNames: readonly string[]): string {
  const names = activeNames.map((n) => String(n ?? '').trim()).filter(Boolean)
  const list = names.length > 0 ? names.join('、') : '无'
  return (
    '连接器的更多工具需先用 `search_tools` 搜索；已激活：' +
    list +
    '。需要远程能力（远程终端/设备/市场等）而当前工具列表没有时，先搜索再调用。'
  )
}

/** 每轮发送给模型的工具组装：非渐进=全量；渐进=非远程工具（含内置与 search_tools）+ 本会话已激活且仍注册的远程工具 */
export function selectRoundTools<T extends { function: { name: string } }>(
  all: readonly T[],
  progressive: boolean,
  activeNames: readonly string[]
): T[] {
  if (!progressive) return [...all]
  const active = new Set(activeNames)
  return all.filter((t) => !isRemoteTool(t.function.name) || active.has(t.function.name))
}

export interface SearchToolsDeps {
  /** 当前已注册的远程工具（禁用/卸载的连接器不在其中） */
  listRemote(): ToolSearchEntry[]
  /** 把命中的工具名合并进本会话激活集并持久化，返回最新激活集 */
  activate(names: string[], sessionId: string): string[]
}

/** 构造 search_tools 工具（kind: read，不触发权限确认）：检索 → 激活 → 文本清单 */
export function createSearchToolsTool(deps: SearchToolsDeps): ToolDefinition {
  return {
    name: SEARCH_TOOLS_NAME,
    description:
      '在已启用连接器的远程工具（MCP / 市场远程能力）中按关键词搜索。命中的工具会立即激活，下一轮对话即可直接调用；' +
      '当需要远程能力（远程终端/设备/市场等）而当前工具列表里没有时，先用本工具搜索。',
    kind: 'read',
    parameters: {
      type: 'object',
      properties: {
        query: { type: 'string', description: '搜索关键词（工具名/功能描述/连接器名，空格分隔多个词）' },
        limit: {
          type: 'integer',
          description: `最多返回条数（1-${SEARCH_TOOLS_MAX_LIMIT}），缺省 ${SEARCH_TOOLS_DEFAULT_LIMIT}`
        }
      },
      required: ['query']
    },
    async execute(args, ctx) {
      const query = typeof args.query === 'string' ? args.query.trim() : ''
      if (!query) return { ok: false, output: '参数 query 必须为非空字符串' }
      try {
        const entries = deps.listRemote()
        const hits = searchRemoteTools(
          entries,
          query,
          typeof args.limit === 'number' ? args.limit : undefined
        )
        if (hits.length === 0) {
          return {
            ok: true,
            output: `未找到匹配「${query}」的远程工具（当前已注册 ${entries.length} 个）。可换关键词重试，或直接用现有工具完成任务。`
          }
        }
        deps.activate(hits.map((h) => h.name), ctx.sessionId)
        return { ok: true, output: formatSearchResult(hits) }
      } catch (err) {
        return { ok: false, output: `工具搜索失败: ${err instanceof Error ? err.message : String(err)}` }
      }
    }
  }
}
