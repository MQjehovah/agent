import { randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { assertSafeCapabilityName } from './installer'

/**
 * 连接器(MCP)启用偏好（重启持久化）：
 *   连接器安装后默认启用；「+」菜单可逐项禁用/启用，只把禁用名单落到
 *   <dataDir>/localagent/mcp-prefs.json（{ "disabled": ["name", ...] }），缺省即启用。
 *   本模块只做偏好文件的纯 IO 与容错解析，可离线单测；连接跳过由 mcp.ts 的
 *   isDisabled 注入（读取本模块的 loadDisabled）。
 */

/** mcp-prefs.json 路径（dataDir = GATEWAY_DATA_DIR） */
export function mcpPrefsFilePath(dataDir: string): string {
  return join(dataDir, 'localagent', 'mcp-prefs.json')
}

/** 读禁用集合：文件缺失→空；JSON 损坏 / 顶层形状非法→空并 console.warn；
 *  非法 / 重复 name 条目跳过（Set 自带去重，不阻断启动与 chat） */
export function loadDisabled(dataDir: string): Set<string> {
  const file = mcpPrefsFilePath(dataDir)
  if (!existsSync(file)) return new Set()
  let parsed: unknown
  try {
    parsed = JSON.parse(readFileSync(file, 'utf8'))
  } catch (err) {
    console.warn(`[kernel] 连接器偏好损坏，按空处理: ${err instanceof Error ? err.message : String(err)}`)
    return new Set()
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    console.warn('[kernel] 连接器偏好顶层非法，按空处理')
    return new Set()
  }
  const disabled = (parsed as { disabled?: unknown }).disabled
  if (!Array.isArray(disabled)) {
    console.warn('[kernel] 连接器偏好缺少 disabled 数组，按空处理')
    return new Set()
  }
  const out = new Set<string>()
  for (const raw of disabled) {
    if (typeof raw !== 'string') continue
    const name = raw.trim()
    try {
      assertSafeCapabilityName(name)
    } catch {
      continue
    }
    out.add(name)
  }
  return out
}

/** 写禁用集合：原子写（同目录临时文件 + rename 覆盖，避免半截 JSON）；
 *  name 去重后按字典序落盘（内容稳定），任一 name 非法抛错、不做坏数据落盘 */
export function saveDisabled(dataDir: string, disabled: Iterable<string>): void {
  const names = [...new Set(disabled)].sort()
  for (const name of names) assertSafeCapabilityName(name)
  const file = mcpPrefsFilePath(dataDir)
  mkdirSync(dirname(file), { recursive: true })
  const tmp = `${file}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`
  writeFileSync(tmp, `${JSON.stringify({ disabled: names }, null, 2)}\n`, 'utf8')
  try {
    renameSync(tmp, file)
  } catch (err) {
    rmSync(tmp, { force: true })
    throw err
  }
}

/** 设置单个连接器禁用状态（读-改-写，enabled=false 即从禁用名单移除），返回最新禁用集合 */
export function setDisabled(dataDir: string, name: string, disabled: boolean): Set<string> {
  assertSafeCapabilityName(name)
  const current = loadDisabled(dataDir)
  if (disabled) current.add(name)
  else current.delete(name)
  saveDisabled(dataDir, current)
  return current
}
