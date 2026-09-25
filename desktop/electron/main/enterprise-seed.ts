/**
 * 首次启动从随包携带的 enterprise.json 补全企业配置(员工端无需手填 OIDC 等)。
 * 依赖注入,不依赖 Electron 便于单测;判定与逐字段判空均基于原始 config.json(stored),
 * 不用合并 DEFAULTS 后的配置,否则默认地址会掩盖「尚未注入」。
 */

import type { AppConfig } from './store'

export const ENTERPRISE_FIELDS = [
  'oidcIssuer',
  'oidcClientId',
  'oidcClientSecret',
  'agentUrl',
  'ragUrl',
  'marketUrl',
  'routerUrl',
  'routerAdminUrl',
  'updateFeedUrl',
  'asrUrl'
] as const

/** 早退判定用字段:client_secret 可选(公共客户端 + PKCE),不参与是否已注入的判断 */
const REQUIRED_SEED_FIELDS = ENTERPRISE_FIELDS.filter((key) => key !== 'oidcClientSecret')

export interface EnterpriseSeedDeps {
  /** 候选 enterprise.json(按优先级) */
  candidates: string[]
  /** config.json 原始内容(未合并默认值) */
  stored: Partial<AppConfig>
  exists: (path: string) => boolean
  readFile: (path: string) => string
  /** 写入补全字段(走 updateConfig 落盘) */
  update: (patch: Partial<AppConfig>) => void
  log?: (message: string) => void
  warn?: (message: string) => void
}

/** 只填空字段,不覆盖用户已保存的配置;找不到文件或解析失败静默跳过。 */
export function seedEnterpriseConfig(deps: EnterpriseSeedDeps): void {
  const { stored } = deps
  // 必需字段已就位:无需读盘注入
  if (REQUIRED_SEED_FIELDS.every((key) => (stored[key] ?? '').trim())) return

  for (const file of deps.candidates) {
    try {
      if (!deps.exists(file)) continue
      const data = JSON.parse(deps.readFile(file)) as Record<string, unknown>
      const patch: Partial<AppConfig> = {}
      for (const key of ENTERPRISE_FIELDS) {
        const value = data[key]
        if (typeof value === 'string' && value.trim() && !(stored[key] ?? '').trim()) {
          patch[key] = value.trim()
        }
      }
      if (Object.keys(patch).length > 0) {
        deps.update(patch)
        deps.log?.(`[config] 已从 enterprise.json 注入: ${Object.keys(patch).join(', ')}`)
      }
      return
    } catch (err) {
      deps.warn?.(`[config] 读取 ${file} 失败: ${(err as Error).message}`)
    }
  }
}
