/**
 * 本机（stdio）连接器环境变量读写：编辑 localagent/mcp.json 单条 server 的 env。
 *
 * 与平台密钥（market 侧、云端注入）分工：这里只影响 dashboard 本机拉起连接器时
 * 注入子进程的变量，例如把包内 ${DINGTALK_APP_SECRET} 占位符覆写成用户自己的值。
 * 内核托管的 ${MARKET_URL}/${MARKET_TOKEN} 一律保留原值（UI 只读，写回忽略覆盖与删除）。
 * 合并语义：edited 值非空 → 写入；空串 → 删除该键；键名非法 → 整批拒绝不落盘。
 * 纯函数 mergeConnectorEnv 可离线单测；IO 沿用 mcp-prefs 的原子写模式。
 */
import { randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { assertSafeCapabilityName } from './installer'

/** 内核托管的占位符值（mcp.json 里按字面量保存，连接时由主进程解析注入） */
const AUTO_ENV_VALUES = ['${MARKET_URL}', '${MARKET_TOKEN}']

/** 环境变量键名：字母/下划线开头，后接字母/数字/下划线（与常见 POSIX 变量一致） */
const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/

export function isValidEnvKey(key: string): boolean {
  return ENV_KEY_RE.test(key)
}

/** 该值是否由内核托管（MARKET_URL / MARKET_TOKEN 占位符） */
export function isAutoEnvValue(value: unknown): boolean {
  const text = String(value ?? '').trim()
  return AUTO_ENV_VALUES.includes(text)
}

/**
 * 合并连接器 env：以 current 为基础，autoVars（或当前值本就是托管占位符的键）
 * 保留原值；其余 edited 键值非空写入、空串删除。键名非法抛中文错（整批拒绝）。
 */
export function mergeConnectorEnv(
  current: Record<string, string>,
  edited: Record<string, string>,
  autoVars: Iterable<string>
): Record<string, string> {
  const auto = new Set(autoVars)
  const out: Record<string, string> = { ...current }
  for (const [rawKey, rawValue] of Object.entries(edited)) {
    const key = rawKey.trim()
    if (!isValidEnvKey(key)) {
      throw new Error(`环境变量名非法：${rawKey}（仅允许字母/数字/下划线，且不以数字开头）`)
    }
    if (auto.has(key) || isAutoEnvValue(current[key])) continue
    const value = String(rawValue ?? '').trim()
    if (value === '') delete out[key]
    else out[key] = value
  }
  return out
}

export interface ConnectorEnvSnapshot {
  /** 当前 env（原样返回，占位符不解析） */
  env: Record<string, string>
  /** 值由内核托管（${MARKET_URL}/${MARKET_TOKEN}）的键，UI 只读展示 */
  autoVars: string[]
}

function mcpConfigFilePath(dataDir: string): string {
  return join(dataDir, 'localagent', 'mcp.json')
}

/** env 只接受普通对象；非字符串值转字符串展示（写回时会被收敛为字符串） */
function normalizeEnv(value: unknown): Record<string, string> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return {}
  const out: Record<string, string> = {}
  for (const [key, raw] of Object.entries(value as Record<string, unknown>)) {
    if (raw === null || raw === undefined) continue
    out[key] = String(raw)
  }
  return out
}

/** 读 mcp.json 顶层；缺失按空配置，损坏/形状非法抛中文错（不改动原文件） */
function readMcpConfig(dataDir: string): { servers: Array<Record<string, unknown>> } {
  const file = mcpConfigFilePath(dataDir)
  if (!existsSync(file)) return { servers: [] }
  let parsed: unknown
  try {
    parsed = JSON.parse(readFileSync(file, 'utf8'))
  } catch (e) {
    throw new Error(`本地 MCP 配置解析失败，未做改动: ${e instanceof Error ? e.message : String(e)}`)
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error('本地 MCP 配置无效，未做改动: 顶层必须是对象')
  }
  const servers = (parsed as { servers?: unknown }).servers
  if (!Array.isArray(servers)) {
    throw new Error('本地 MCP 配置无效，未做改动: servers 必须是数组')
  }
  return { servers: servers as Array<Record<string, unknown>> }
}

/** 原子写回（同目录临时文件 + rename），避免半截 JSON */
function writeMcpConfig(dataDir: string, config: { servers: Array<Record<string, unknown>> }): void {
  const file = mcpConfigFilePath(dataDir)
  mkdirSync(dirname(file), { recursive: true })
  const tmp = `${file}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`
  writeFileSync(tmp, `${JSON.stringify(config, null, 2)}\n`, 'utf8')
  try {
    renameSync(tmp, file)
  } catch (err) {
    rmSync(tmp, { force: true })
    throw err
  }
}

/** 读某连接器当前 env 快照；连接器不存在抛中文错 */
export function loadConnectorEnv(dataDir: string, name: string): ConnectorEnvSnapshot {
  assertSafeCapabilityName(name)
  const { servers } = readMcpConfig(dataDir)
  const server = servers.find((item) => item?.name === name)
  if (!server) throw new Error(`未找到连接器 ${name}（可能已被卸载）`)
  const env = normalizeEnv(server.env)
  return { env, autoVars: Object.keys(env).filter((key) => isAutoEnvValue(env[key])) }
}

/** 写回某连接器 env（保留 auto 原值、空值删除、非法键拒绝）；连接器不存在抛中文错 */
export function saveConnectorEnv(
  dataDir: string,
  name: string,
  edited: Record<string, string>
): void {
  assertSafeCapabilityName(name)
  const config = readMcpConfig(dataDir)
  const server = config.servers.find((item) => item?.name === name)
  if (!server) throw new Error(`未找到连接器 ${name}（可能已被卸载）`)
  const current = normalizeEnv(server.env)
  const autoVars = Object.keys(current).filter((key) => isAutoEnvValue(current[key]))
  server.env = mergeConnectorEnv(current, edited, autoVars)
  writeMcpConfig(dataDir, config)
}
