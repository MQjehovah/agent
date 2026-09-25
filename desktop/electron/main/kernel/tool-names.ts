/**
 * 工具名收敛：内核本地工具名（file_read / mcp__<server>__<tool> / market:<name> 等）
 * 与 provider（OpenAI 兼容网关/DeepSeek 等）要求的 `^[a-zA-Z0-9_-]{1,64}$` 之间的映射。
 * 纯函数，无 IO，便于单测；映射不落盘，持久化与 UI 一律保留本地名。
 */

/** provider 允许的工具名：字符集 + DeepSeek 文档的 64 长度上限 */
const MAX_PROVIDER_NAME = 64
const PROVIDER_NAME_RE = new RegExp(`^[a-zA-Z0-9_-]{1,${MAX_PROVIDER_NAME}}$`)

/** 非法字符替换为 _，超长截断到 64；空串回退 'tool'（保证结果恒满足 provider 合法名） */
export function toProviderToolName(name: string): string {
  if (PROVIDER_NAME_RE.test(name)) return name
  const sanitized = name.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, MAX_PROVIDER_NAME)
  return sanitized.length > 0 ? sanitized : 'tool'
}

/**
 * 依据注册表工具名列表构建双向映射：
 * - 已合法的名字恒等（除非该名字被更早的非法名 sanitize 后先占用）；
 * - 非法名 sanitize（含 64 截断）为合法名，冲突时追加 _2/_3… 直到唯一（截掉尾部腾出后缀位，结果仍 ≤64）；
 * 返回的 toProvider 用于请求侧改写，toLocal 用于把模型回传的 provider 名解析回本地工具。
 *
 * 已知限制（历史名兜底，不做代码处理）：
 * - 历史里已删除工具的兜底 sanitize 名可能与现存工具 provider 名相撞，此时会解析到现存工具；
 * - 多个旧名 sanitize/截断后可能塌缩为同一个 provider 名，wire 层无法区分。
 */
export function buildToolNameMaps(localNames: string[]): {
  toProvider: Map<string, string>
  toLocal: Map<string, string>
} {
  const toProvider = new Map<string, string>()
  const toLocal = new Map<string, string>()
  const used = new Set<string>()
  for (const local of localNames) {
    if (toProvider.has(local)) continue
    const base = toProviderToolName(local)
    let provider = base
    let suffix = 2
    while (used.has(provider)) {
      const tag = `_${suffix++}`
      provider = base.slice(0, MAX_PROVIDER_NAME - tag.length) + tag
    }
    used.add(provider)
    toProvider.set(local, provider)
    toLocal.set(provider, local)
  }
  return { toProvider, toLocal }
}
