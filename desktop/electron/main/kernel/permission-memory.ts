import { randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

/**
 * 本地工具权限记忆（跨会话/重启永久免问）：
 *   用户在权限确认框选择「允许并记住此工具」后，工具名落到
 *   <dataDir>/localagent/permission-memory.json（{ "alwaysAllow": ["..."] }）。
 *   permissions.ts 在 needsAsk/ask 前经注入的 has/add 短路，设置页可查看/忘记/清空。
 *   本模块只做偏好文件的纯 IO 与容错解析，可离线单测。
 */

/** permission-memory.json 路径（dataDir = GATEWAY_DATA_DIR） */
export function permissionMemoryFilePath(dataDir: string): string {
  return join(dataDir, 'localagent', 'permission-memory.json')
}

/** 校验并规范化工具名：trim 后非空、长度 ≤128、无控制字符；非法返回 null */
function normalizeTool(raw: unknown): string | null {
  if (typeof raw !== 'string') return null
  const tool = raw.trim()
  if (!tool || tool.length > 128) return null
  if (/[\u0000-\u001f\u007f]/.test(tool)) return null
  return tool
}

/** 写前校验：返回规范化后的工具名，非法抛错（不做坏数据落盘） */
function assertTool(raw: unknown): string {
  const tool = normalizeTool(raw)
  if (!tool) throw new Error('非法工具名')
  return tool
}

/** 原子写（同目录临时文件 + rename 覆盖，避免半截 JSON）；去重后按字典序落盘（内容稳定） */
function saveAlwaysAllowed(dataDir: string, tools: Iterable<string>): void {
  const names = [...new Set(tools)].sort()
  const file = permissionMemoryFilePath(dataDir)
  mkdirSync(dirname(file), { recursive: true })
  const tmp = `${file}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`
  writeFileSync(tmp, `${JSON.stringify({ alwaysAllow: names }, null, 2)}\n`, 'utf8')
  try {
    renameSync(tmp, file)
  } catch (err) {
    rmSync(tmp, { force: true })
    throw err
  }
}

/** 读永久免问集合：文件缺失→空；JSON 损坏 / 顶层形状非法→空并 console.warn；
 *  非法 / 重复条目跳过（Set 自带去重，不阻断启动与 chat） */
export function loadAlwaysAllowed(dataDir: string): Set<string> {
  const file = permissionMemoryFilePath(dataDir)
  if (!existsSync(file)) return new Set()
  let parsed: unknown
  try {
    parsed = JSON.parse(readFileSync(file, 'utf8'))
  } catch (err) {
    console.warn(`[kernel] 权限记忆损坏，按空处理: ${err instanceof Error ? err.message : String(err)}`)
    return new Set()
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    console.warn('[kernel] 权限记忆顶层非法，按空处理')
    return new Set()
  }
  const alwaysAllow = (parsed as { alwaysAllow?: unknown }).alwaysAllow
  if (!Array.isArray(alwaysAllow)) {
    console.warn('[kernel] 权限记忆缺少 alwaysAllow 数组，按空处理')
    return new Set()
  }
  const out = new Set<string>()
  for (const raw of alwaysAllow) {
    const tool = normalizeTool(raw)
    if (tool) out.add(tool)
  }
  return out
}

/** 列出永久免问工具（字典序；读盘容错同 loadAlwaysAllowed） */
export function listAlwaysAllowed(dataDir: string): string[] {
  return [...loadAlwaysAllowed(dataDir)].sort()
}

/** 追加一个永久免问工具（读-改-写；已存在则幂等） */
export function addAlwaysAllowed(dataDir: string, tool: string): void {
  const name = assertTool(tool)
  const current = loadAlwaysAllowed(dataDir)
  current.add(name)
  saveAlwaysAllowed(dataDir, current)
}

/** 移除一个永久免问工具（读-改-写；不存在则幂等） */
export function removeAlwaysAllowed(dataDir: string, tool: string): void {
  const name = assertTool(tool)
  const current = loadAlwaysAllowed(dataDir)
  current.delete(name)
  saveAlwaysAllowed(dataDir, current)
}

/** 清空全部永久免问工具（落盘 alwaysAllow: []，文件保留） */
export function clearAlwaysAllowed(dataDir: string): void {
  saveAlwaysAllowed(dataDir, [])
}
