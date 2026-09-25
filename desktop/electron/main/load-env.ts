/**
 * 启动时加载 `.env`（仅补缺失，不覆盖已存在的进程环境变量）。
 *
 * 用途：开发 / 内网运行时便捷注入凭据与地址（见 `.env.example`），免去逐个 export：
 *   AGENT_SERVICE_TOKEN / AGENT_ADMIN_USER / AGENT_ADMIN_PASSWORD
 *   OIDC_ISSUER / OIDC_CLIENT_ID / OIDC_CLIENT_SECRET
 *   AGENT_URL / RAG_URL / MARKET_URL / ROUTER_URL / ROUTER_ADMIN_URL
 *
 * 查找顺序：进程 cwd/.env → 项目根（相对 out/main 的 ../../）.env；找不到静默跳过。
 * 打包版请用随包 enterprise.json 或系统环境变量。
 */
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

function applyDotEnv(path: string): void {
  let text: string
  try {
    text = readFileSync(path, 'utf-8')
  } catch {
    return
  }
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#')) continue
    const eq = line.indexOf('=')
    if (eq <= 0) continue
    const key = line.slice(0, eq).trim()
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue
    let value = line.slice(eq + 1).trim()
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1)
    }
    // 已存在的进程环境变量优先（不覆盖）
    if (process.env[key] === undefined) process.env[key] = value
  }
}

const here = typeof __dirname !== 'undefined' ? __dirname : ''
const candidates = [join(process.cwd(), '.env'), here ? join(here, '..', '..', '.env') : ''].filter(
  (p): p is string => Boolean(p)
)

for (const file of candidates) {
  if (existsSync(file)) {
    applyDotEnv(file)
    break
  }
}
