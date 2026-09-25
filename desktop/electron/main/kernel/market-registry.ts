import { randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { assertSafeCapabilityName } from './installer'

/**
 * 市场远程 tool 的本地安装清单（重启持久化）：
 *   type=tool 的能力不下载代码，只在 registry 注册 market:<name> 远程适配工具（云端 runtime 执行）。
 *   进程内注册随重启消失，因此把「装过哪些 tool」落盘到 <dataDir>/localagent/market-tools.json，
 *   首次 chat / 重建 registry 时读回并重新注册。本模块只做清单文件的纯 IO 与容错解析，
 *   可离线单测；注册动作由 ipc 层负责。
 */

/** 清单单条记录：name 必填，version/description 可选（对齐市场「我的能力」条目） */
export interface MarketToolRecord {
  name: string
  version?: string
  description?: string
}

/** market-tools.json 路径（dataDir = GATEWAY_DATA_DIR） */
export function marketToolsFilePath(dataDir: string): string {
  return join(dataDir, 'localagent', 'market-tools.json')
}

/** 单条原始记录容错解析：非对象 / name 缺失或非法（防逃逸校验）返回 null */
function toRecord(raw: unknown): MarketToolRecord | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null
  const r = raw as Record<string, unknown>
  const name = typeof r.name === 'string' ? r.name.trim() : ''
  try {
    assertSafeCapabilityName(name)
  } catch {
    return null
  }
  const rec: MarketToolRecord = { name }
  const version = typeof r.version === 'string' && r.version.trim() ? r.version.trim() : ''
  if (version) rec.version = version
  const description = typeof r.description === 'string' && r.description.trim() ? r.description.trim() : ''
  if (description) rec.description = description
  return rec
}

/** 读安装清单：文件缺失→空；JSON 损坏 / 顶层形状非法→空并 console.warn（不阻断启动与 chat） */
export function loadMarketToolsFile(dataDir: string): MarketToolRecord[] {
  const file = marketToolsFilePath(dataDir)
  if (!existsSync(file)) return []
  let parsed: unknown
  try {
    parsed = JSON.parse(readFileSync(file, 'utf8'))
  } catch (err) {
    console.warn(`[kernel] 市场工具清单损坏，按空处理: ${err instanceof Error ? err.message : String(err)}`)
    return []
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    console.warn('[kernel] 市场工具清单顶层非法，按空处理')
    return []
  }
  const tools = (parsed as { tools?: unknown }).tools
  if (!Array.isArray(tools)) {
    console.warn('[kernel] 市场工具清单缺少 tools 数组，按空处理')
    return []
  }
  const out: MarketToolRecord[] = []
  for (const raw of tools) {
    const rec = toRecord(raw)
    if (rec) out.push(rec)
  }
  return out
}

/** 写安装清单：原子写（同目录临时文件 + rename 覆盖，避免半截 JSON）；任一 name 非法抛错，不做坏数据落盘 */
export function saveMarketToolsFile(dataDir: string, tools: MarketToolRecord[]): void {
  for (const rec of tools) assertSafeCapabilityName(rec.name)
  const file = marketToolsFilePath(dataDir)
  mkdirSync(dirname(file), { recursive: true })
  const tmp = `${file}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`
  writeFileSync(tmp, `${JSON.stringify({ tools }, null, 2)}\n`, 'utf8')
  try {
    renameSync(tmp, file)
  } catch (err) {
    rmSync(tmp, { force: true })
    throw err
  }
}
