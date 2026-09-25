/**
 * 本地能力安装器:把 market 下载的能力包安全地落到 GATEWAY_DATA_DIR/localagent 下。
 *   skill → localagent/skills/<name>/SKILL.md(缺 front-matter 则补 name/description)
 *   mcp   → 读包内 connection.json,写/更新 localagent/mcp.json(server 名 = 能力名,覆盖同名、保留其它)
 *   agent → agent.json + PROMPT.md(+TEAM.md) → localagent/agents/<name>/
 *   tool  → 不落盘,走市场侧远程适配(见市场页说明)
 * 一切异常折叠为 {ok,output} 返回,不向外抛;zip 解包防 zip-slip。
 */
import { existsSync, readdirSync, rmSync, statSync, type Dirent } from 'node:fs'
import { mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { dirname, join, resolve, sep } from 'node:path'
import { unzipSync } from 'fflate'
import type { MarketCapabilityType, MarketRuntime } from './market'

/** MCP 安装模式:platform=平台桥接(经平台 MCP 网关)、local=本地安装(本机直接拉起) */
export type McpInstallMode = 'platform' | 'local'

/** dataDir 参数 = GATEWAY_DATA_DIR;本模块不读 env,一律由调用方传入 */
export interface InstallCapabilityOptions {
  dataDir: string
  type: MarketCapabilityType
  name: string
  version: string
  /** 下载得到的能力包 zip 原始字节 */
  artifact: Buffer
  description?: string
  /** 仅 mcp 生效:指定安装模式;未传保持旧的自动判定行为(向后兼容) */
  mode?: McpInstallMode
  /** 本地 stdio MCP 的显式确认(展示命令后由用户确认);未确认则返回 consent 预览,不写配置 */
  confirmed?: boolean
}

export interface UninstallCapabilityOptions {
  dataDir: string
  type: MarketCapabilityType
  name: string
  /** 仅 mcp 生效:测试注入目录删除实现(缺省 removeDirWithRetry,真实删除带重试) */
  removeDirImpl?: (dir: string) => Promise<RemoveDirResult>
}

/** 本地执行命令预览:安装本地 stdio MCP 前需用户显式确认(MCP 安全最佳实践) */
export interface InstallConsent {
  command: string
  args: string[]
  envKeys: string[]
  cwd?: string
}

/** 未确认即安装本地可执行 MCP 时抛出:携带命令预览,由上层提示用户确认后重试(不写配置) */
export class ConsentRequiredError extends Error {
  consent: InstallConsent
  constructor(consent: InstallConsent) {
    super('本地安装需要确认将要执行的命令')
    this.name = 'ConsentRequiredError'
    this.consent = consent
  }
}

export interface InstallResult {
  ok: boolean
  output: string
  /** 仅本地 stdio MCP 且未确认时返回:待用户确认的命令预览 */
  consent?: InstallConsent
}

// ---- 目录布局 ----

function skillsRoot(dataDir: string): string {
  return join(dataDir, 'localagent', 'skills')
}

function agentsRoot(dataDir: string): string {
  return join(dataDir, 'localagent', 'agents')
}

function mcpConfigPath(dataDir: string): string {
  return join(dataDir, 'localagent', 'mcp.json')
}

/** 本地安装的 MCP 实现文件解压目录(mcp-servers/<能力名>) */
function mcpServersRoot(dataDir: string): string {
  return join(dataDir, 'localagent', 'mcp-servers')
}

// ---- 名称 / zip 路径安全 ----

/** 能力名安全校验:名称会拼进目录与 mcp.json server 名,拒绝路径分隔符/`.`/`..` 防逃逸,
 *  并拒绝控制字符与 Windows 非法字符 `\/:*?"<>|`,避免落到 raw ENOENT。
 *  导出供 market-registry.ts 复用(市场 tool 清单的 name 同样会进注册表名) */
export function assertSafeCapabilityName(name: string): void {
  if (typeof name !== 'string' || name.length === 0 || name.length > 128) throw new Error('非法能力名')
  if (name === '.' || name === '..') throw new Error('非法能力名')
  if (/[\\/:*?"<>|\u0000-\u001f\u007f]/.test(name)) {
    throw new Error('非法能力名（不能含路径分隔符、控制字符或 Windows 非法字符）')
  }
}

/** zip 条目路径校验:拒绝绝对路径、盘符、`..` 段(zip-slip),条目录统一当 `/` 分隔 */
function assertSafeEntryName(name: string): void {
  if (name.length === 0) throw new Error('能力包包含非法路径条目（空文件名）')
  const norm = name.replace(/\\/g, '/')
  if (norm.startsWith('/')) throw new Error(`能力包包含非法路径条目：${name}`)
  if (/^[A-Za-z]:/.test(norm)) throw new Error(`能力包包含非法路径条目：${name}`)
  if (norm.split('/').some((s) => s === '..')) throw new Error(`能力包包含非法路径条目：${name}`)
}

/**
 * 安全解包:解压全部文件条目到 destDir(目录条目跳过,嵌套目录自动创建),
 * 返回写出的条目名列表。任一条目非法即整体拒绝(抛中文错,不写任何文件)。
 */
export async function unzipSafe(bytes: Uint8Array, destDir: string): Promise<string[]> {
  const destAbs = resolve(destDir)
  const written: string[] = []
  for (const [name, data] of openZip(bytes)) {
    const norm = name.replace(/\\/g, '/')
    if (norm.endsWith('/')) continue // 纯目录条目,文件落盘时自动建目录
    const segs = norm.split('/')
    const target = resolve(destAbs, ...segs)
    // 二次兜底:resolve 后必须仍落在 destDir 内
    if (!(target === destAbs || target.startsWith(destAbs + sep))) {
      throw new Error(`能力包包含非法路径条目：${name}`)
    }
    await mkdir(dirname(target), { recursive: true })
    await writeFile(target, data)
    written.push(norm)
  }
  return written
}

/**
 * 解压到内存并做同名路径校验(不落盘):返回 条目名 → bytes。
 * 校验先于任何使用,zip-slip 包在读取阶段即被整体拒绝。
 */
function openZip(bytes: Uint8Array): Map<string, Uint8Array> {
  let files: Record<string, Uint8Array>
  try {
    files = unzipSync(bytes)
  } catch (e) {
    throw new Error(`能力包解压失败: ${e instanceof Error ? e.message : String(e)}`)
  }
  const out = new Map<string, Uint8Array>()
  for (const name of Object.keys(files)) {
    assertSafeEntryName(name)
    out.set(name, files[name])
  }
  return out
}

// ---- skill ----

/**
 * 保证 SKILL.md 带 name/description front-matter(与 skills.ts 加载器约定一致):
 * - 无 front-matter → 文件头补一块 name/description
 * - 有 front-matter 但缺 name/description,或 key 存在但值空(与 skills.ts 一致,去引号后为空)
 *   → 只补缺失/空值键的缺省值,已有内容不动
 * - 两者齐全且值非空 → 原样返回,不改动
 */
function ensureSkillFrontMatter(opts: { name: string; description: string; content: string }): string {
  const { name, description, content } = opts
  const hasBom = content.charCodeAt(0) === 0xfeff
  const body = hasBom ? content.slice(1) : content
  const lines = body.split(/\r?\n/)
  if (lines[0]?.trim() !== '---') {
    return `---\nname: ${name}\ndescription: ${description}\n---\n${hasBom ? content : body}`
  }
  // 已有开围栏,但没找到闭合围栏 → 视为无 front-matter,整块补写
  let close = -1
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === '---') {
      close = i
      break
    }
  }
  if (close === -1) {
    return `---\nname: ${name}\ndescription: ${description}\n---\n${hasBom ? content : body}`
  }
  // 值语义与 skills.ts 加载器一致：key 需存在且去掉成对引号后仍有内容才算有效，空值/缺键都视为缺失
  const keyLine = (k: string): number => {
    const re = new RegExp(`^${k}\\s*:`)
    for (let i = 1; i < close; i++) if (re.test(lines[i])) return i
    return -1
  }
  const keyHasValue = (k: string): boolean => {
    const i = keyLine(k)
    if (i === -1) return false
    const value = lines[i].slice(lines[i].indexOf(':') + 1).trim().replace(/^["']|["']$/g, '')
    return value.length > 0
  }
  const fill = (k: string, fallback: string): void => {
    const i = keyLine(k)
    if (i === -1) {
      lines.splice(close, 0, `${k}: ${fallback}`)
      close += 1
    } else {
      // 存在但值为空的 key 就地改写该行，不追加第二行(加载器只认首个 key，追加会仍取到空值)
      lines[i] = `${k}: ${fallback}`
    }
  }
  const needName = !keyHasValue('name')
  const needDescription = !keyHasValue('description')
  if (!needName && !needDescription) return content
  if (needName) fill('name', name)
  if (needDescription) fill('description', description)
  return lines.join('\n')
}

async function installSkill(dataDir: string, opts: InstallCapabilityOptions): Promise<void> {
  assertSafeCapabilityName(opts.name)
  const dir = join(skillsRoot(dataDir), opts.name)
  await unzipSafe(opts.artifact, dir)
  const skillPath = join(dir, 'SKILL.md')
  let raw: string
  try {
    raw = await readFile(skillPath, 'utf8')
  } catch {
    throw new Error('技能包缺少 SKILL.md')
  }
  const description = opts.description?.trim() || '市场安装的技能'
  const ensured = ensureSkillFrontMatter({ name: opts.name, description, content: raw })
  if (ensured !== raw) await writeFile(skillPath, ensured, 'utf8')
}

// ---- mcp ----

/** mcp.json 里单条 server 的宽结构(含来源标记;内核 parseMcpConfig 读取 name/command/url/args/transport/headers/env/cwd/kind) */
interface McpServerItem {
  name: string
  source: string
  command?: string
  args?: string[]
  env?: Record<string, string>
  cwd?: string
  url?: string
  transport?: 'http'
  headers?: Record<string, string>
  kind?: 'market-remote' | 'market-gateway'
  [k: string]: unknown
}

function nonEmptyStr(v: unknown): string | null {
  return typeof v === 'string' && v.trim().length > 0 ? v.trim() : null
}

/** headers 缺省/空对象视为无自定义头;其余(非空或类型不明)视为本地无法透传 */
function headersEmpty(conn: Record<string, unknown>): boolean {
  return (
    conn.headers === undefined ||
    conn.headers === null ||
    (typeof conn.headers === 'object' && !Array.isArray(conn.headers) && Object.keys(conn.headers).length === 0)
  )
}

/** env 必须是 string→string 的普通对象,否则 null(视为该条配置非法) */
function normalizeEnvObject(v: unknown): Record<string, string> | null {
  if (typeof v !== 'object' || v === null || Array.isArray(v)) return null
  const out: Record<string, string> = {}
  for (const [key, value] of Object.entries(v as Record<string, unknown>)) {
    if (typeof value !== 'string') return null
    out[key] = value
  }
  return out
}

/** 判定 args 里是否引用包内实现文件:这类 stdio 服务依赖代码落盘 + 环境,本地无法直接 spawn */
function isPackageScriptRef(arg: string): boolean {
  const norm = arg.replace(/\\/g, '/')
  if (arg.startsWith('implementation/') || norm.startsWith('implementation/')) return true
  if (/^[A-Za-z]:/.test(norm) || norm.startsWith('/')) return false // 绝对路径是机器已有文件
  if (arg.startsWith('-')) return false // 命令行开关
  return /\.(py|js|mjs|cjs|ts)$/i.test(arg)
}

/**
 * connection.json → mcp.json 条目。本地内核可直接拉起的形态：
 * - stdio(command+args,无 env/cwd/包内实现文件依赖)
 * - sse(url,无自定义 headers)
 * - streamable HTTP(url,无自定义 headers → transport:'http')
 * - gateway → kind:'market-gateway',运行时由 ipc 用 marketUrl+SSO token 解析能力端点
 * 凡依赖 env / 包内实现文件 / 自定义 headers 的连接仍记 `kind: 'market-remote'`,由市场侧远程适配。
 * transport 非已知集合时抛错(视为坏包)。
 */
function mcpEntryFromConnection(
  conn: Record<string, unknown>,
  name: string,
  version: string
): McpServerItem {
  const base: McpServerItem = { name, source: `capability: ${name}@${version}` }
  const transportRaw = conn.transport
  const transport =
    (typeof transportRaw === 'string' && transportRaw.trim() ? transportRaw.trim() : 'stdio').toLowerCase()
  const headersAreEmpty = headersEmpty(conn)
  switch (transport) {
    case 'stdio': {
      const command = nonEmptyStr(conn.command)
      const envEmpty = conn.env === undefined || conn.env === null ||
        (typeof conn.env === 'object' && !Array.isArray(conn.env) && Object.keys(conn.env).length === 0)
      const cwdEmpty = conn.cwd === undefined || conn.cwd === null || String(conn.cwd).trim() === ''
      const args = conn.args
      const argsOk =
        args === undefined ||
        args === null ||
        (Array.isArray(args) && args.every((a) => typeof a === 'string'))
      const needPackageCode =
        Array.isArray(args) && args.some((a) => typeof a === 'string' && isPackageScriptRef(a))
      if (command && envEmpty && cwdEmpty && argsOk && !needPackageCode) {
        const item: McpServerItem = { ...base, command }
        if (Array.isArray(args) && args.length > 0) item.args = [...(args as string[])]
        return item
      }
      return { ...base, kind: 'market-remote' }
    }
    // streamable HTTP/JSON-RPC over POST:无自定义 headers 时本地内核可直连(内核 transport:'http')
    case 'http':
    case 'streamable_http': {
      const url = nonEmptyStr(conn.url)
      if (url && headersAreEmpty) return { ...base, url, transport: 'http' }
      return { ...base, kind: 'market-remote' }
    }
    case 'sse': {
      const url = nonEmptyStr(conn.url)
      if (url && headersAreEmpty) return { ...base, url }
      return { ...base, kind: 'market-remote' }
    }
    // gateway(自动判定旧路径):仍写 kind:'market-gateway' 供内核 resolveGateway 读取兼容;
    // 平台模式(mode='platform')已改为标准占位符条目,不再写 kind。
    case 'gateway':
      return { ...base, kind: 'market-gateway' }
    default:
      throw new Error(`connection.json 的 transport 不受支持: ${transport}`)
  }
}

/**
 * 平台桥接条目(标准形态):不落具体地址与令牌,url/headers 使用运行时占位符
 * ${MARKET_URL}/${MARKET_TOKEN},内核连接前由主进程注入(见 mcp.ts substituteServerVars)。
 */
function platformBridgeEntry(base: McpServerItem): McpServerItem {
  return {
    ...base,
    transport: 'http',
    url: `\${MARKET_URL}/api/mcp-gateway/relay/${encodeURIComponent(base.name)}/stream`,
    headers: { Authorization: 'Bearer ${MARKET_TOKEN}' }
  }
}

// ---- mcp 本地安装(mode='local') ----

/** 本地安装无可行形态时的统一提示 */
const NO_LOCAL_IMPL = '该能力包未提供本地实现，仅支持云端托管'

/** 解压目录内按文件名递归查找(限定在 root 内,文件名来自已校验的 zip 条目/args) */
function findFilesByName(root: string, base: string): string[] {
  const out: string[] = []
  const walk = (dir: string): void => {
    let entries: Dirent[]
    try {
      entries = readdirSync(dir, { withFileTypes: true })
    } catch {
      return
    }
    for (const entry of entries) {
      const full = join(dir, entry.name)
      if (entry.isDirectory()) walk(full)
      else if (entry.isFile() && entry.name === base) out.push(full)
    }
  }
  walk(root)
  return out
}

/**
 * 把包内 `implementation/**` 与 args 引用的包内相对路径文件解压到 destDir(保留相对目录结构)。
 * 条目名已由 openZip 做过 zip-slip 校验,这里 resolve 后再做一次目录内兜底。
 */
async function extractLocalPackageFiles(
  files: Map<string, Uint8Array>,
  destDir: string,
  args: string[]
): Promise<void> {
  const argRefs = new Set(
    args
      .map((a) => a.replace(/\\/g, '/'))
      .filter((a) => !a.startsWith('-') && !/^[A-Za-z]:/.test(a) && !a.startsWith('/'))
  )
  const destAbs = resolve(destDir)
  for (const [rawName, data] of files) {
    const norm = rawName.replace(/\\/g, '/')
    let rel: string | null = null
    if (norm.startsWith('implementation/')) rel = norm.slice('implementation/'.length)
    else if (argRefs.has(norm)) rel = norm
    if (!rel) continue
    const target = resolve(destAbs, ...rel.split('/'))
    if (!(target === destAbs || target.startsWith(destAbs + sep))) continue
    await mkdir(dirname(target), { recursive: true })
    await writeFile(target, data)
  }
}

/**
 * args 中的包内相对脚本路径 → 解压目录下绝对路径(与市场侧 rewrite_stdio_script_arg 语义一致):
 * implementation 相对路径优先 → 解压目录同名文件 → 按 basename 递归唯一匹配;再找不到抛错。
 * 开关/绝对路径/非脚本后缀原样保留。
 */
function rewriteLocalScriptArg(dir: string, arg: string): string {
  const norm = arg.replace(/\\/g, '/')
  if (norm.startsWith('-')) return arg
  if (/^[A-Za-z]:/.test(norm) || norm.startsWith('/')) return arg
  if (!/\.(py|js|mjs|cjs|ts)$/i.test(norm)) return arg
  const rel = norm.startsWith('implementation/') ? norm.slice('implementation/'.length) : norm
  const cand = resolve(dir, ...rel.split('/'))
  if (existsSync(cand) && statSync(cand).isFile()) return cand
  const base = rel.split('/').pop() ?? rel
  const byName = join(dir, base)
  if (existsSync(byName) && statSync(byName).isFile()) return byName
  const matches = findFilesByName(dir, base)
  if (matches.length === 1) return matches[0]
  throw new Error(`本地安装失败: 能力包中找不到脚本文件 ${arg}`)
}

/**
 * local 模式:按包内 connection.json 生成本地可直接拉起的条目。
 * - stdio:command 必填;args 引用包内实现(implementation/** 或包内相对脚本路径)时,
 *   解压到 localagent/mcp-servers/<name>/ 并把 args 改写为绝对路径,cwd 缺省指向解压目录;
 *   env 原样写入(含 ${VAR} 占位符保留,本地安装不解析占位符)。
 * - http/streamable_http/sse:url 必填且无自定义 headers。
 * - gateway 或信息不足 → 抛「该能力包未提供本地实现，仅支持平台桥接」。
 */
async function localMcpEntry(
  dataDir: string,
  conn: Record<string, unknown>,
  base: McpServerItem,
  files: Map<string, Uint8Array>
): Promise<McpServerItem> {
  const transportRaw = conn.transport
  const transport =
    (typeof transportRaw === 'string' && transportRaw.trim() ? transportRaw.trim() : 'stdio').toLowerCase()
  switch (transport) {
    case 'stdio': {
      const command = nonEmptyStr(conn.command)
      if (!command) throw new Error(NO_LOCAL_IMPL)
      const argsRaw = conn.args
      if (argsRaw !== undefined && argsRaw !== null &&
          !(Array.isArray(argsRaw) && argsRaw.every((a) => typeof a === 'string'))) {
        throw new Error('connection.json 的 args 必须是字符串数组')
      }
      const args = Array.isArray(argsRaw) ? [...(argsRaw as string[])] : []
      let env: Record<string, string> | undefined
      if (conn.env !== undefined && conn.env !== null) {
        const normalized = normalizeEnvObject(conn.env)
        if (!normalized) throw new Error('connection.json 的 env 必须是字符串到字符串的对象')
        if (Object.keys(normalized).length > 0) env = normalized
      }
      let cwd = nonEmptyStr(conn.cwd) ?? undefined
      const needsPackageCode =
        args.some(isPackageScriptRef) ||
        [...files.keys()].some((k) => k.replace(/\\/g, '/').startsWith('implementation/'))
      const item: McpServerItem = { ...base, command }
      if (args.length > 0) item.args = args
      if (env) item.env = env
      if (needsPackageCode) {
        const dir = join(mcpServersRoot(dataDir), base.name)
        await rm(dir, { recursive: true, force: true })
        await mkdir(dir, { recursive: true })
        await extractLocalPackageFiles(files, dir, args)
        if (args.length > 0) item.args = args.map((a) => rewriteLocalScriptArg(dir, a))
        cwd = cwd ?? dir
      }
      if (cwd) item.cwd = cwd
      return item
    }
    case 'http':
    case 'streamable_http': {
      const url = nonEmptyStr(conn.url)
      if (!url || !headersEmpty(conn)) throw new Error(NO_LOCAL_IMPL)
      return { ...base, url, transport: 'http' }
    }
    case 'sse': {
      const url = nonEmptyStr(conn.url)
      if (!url || !headersEmpty(conn)) throw new Error(NO_LOCAL_IMPL)
      return { ...base, url }
    }
    case 'gateway':
      throw new Error(NO_LOCAL_IMPL)
    case 'package':
      // 标准 server.json 仅声明 packages(npm/pypi/oci): 本地不执行外部包(供应链安全)
      throw new Error(NO_LOCAL_IMPL)
    default:
      throw new Error(`connection.json 的 transport 不受支持: ${transport}`)
  }
}

/** 解析既有 mcp.json;缺失返回空配置,损坏抛中文错(不改动原文件) */
async function loadMcpConfig(configPath: string): Promise<{ servers: unknown[] }> {
  if (!existsSync(configPath)) return { servers: [] }
  let parsed: unknown
  try {
    parsed = JSON.parse(await readFile(configPath, 'utf8'))
  } catch (e) {
    throw new Error(`本地 MCP 配置解析失败，未做改动: ${e instanceof Error ? e.message : String(e)}`)
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error('本地 MCP 配置无效，未做改动: 顶层必须是对象')
  }
  const servers = (parsed as { servers?: unknown }).servers
  if (!Array.isArray(servers)) throw new Error('本地 MCP 配置无效，未做改动: servers 必须是数组')
  return parsed as { servers: unknown[] }
}

/** 合并写回:去掉与能力名同名的旧条目后追加新条目(覆盖同名、保留其它),JSON 2 空格缩进 */
async function saveMcpConfig(configPath: string, servers: unknown[]): Promise<void> {
  await mkdir(dirname(configPath), { recursive: true })
  await writeFile(configPath, `${JSON.stringify({ servers }, null, 2)}\n`, 'utf8')
}

/**
 * 标准 server.json → 内部 connection(标准优先; 与 market backend 归一语义一致)。
 * - remotes[0] → http 类连接(transport 归一为下划线, 供 localMcpEntry 识别);
 * - 仅 packages(无 remotes) → transport:'package'(本地不执行外部包, 由 localMcpEntry 提示云端)。
 */
function connectionFromServerJson(server: Record<string, unknown>): Record<string, unknown> {
  const remotes = Array.isArray(server.remotes) ? server.remotes : []
  for (const r of remotes) {
    if (r && typeof r === 'object' && typeof (r as { url?: unknown }).url === 'string') {
      const raw = String((r as { type?: unknown }).type || 'streamable-http').toLowerCase()
      const conn: Record<string, unknown> = {
        transport: raw.replace(/-/g, '_'),
        url: (r as { url: string }).url
      }
      const headers = (r as { headers?: unknown }).headers
      if (headers && typeof headers === 'object') conn.headers = headers
      return conn
    }
  }
  const packages = Array.isArray(server.packages) ? server.packages : []
  if (packages.length > 0) return { transport: 'package', package: packages[0] }
  return { transport: 'stdio' }
}

/**
 * 安装 MCP:解析 connection.json(旧) 或 server.json(标准) 后按 mode 生成条目
 * (一律覆盖同名、保留其它):
 * - mode='platform' → 平台桥接,不论包内 transport 一律写标准占位符条目;
 * - mode='local'    → 本地安装(见 localMcpEntry),无可行形态时报中文错;
 * - mode 未传       → 沿用旧的自动判定(mcpEntryFromConnection),向后兼容。
 */
async function installMcp(
  dataDir: string,
  opts: InstallCapabilityOptions,
  mode?: McpInstallMode
): Promise<McpServerItem> {
  assertSafeCapabilityName(opts.name)
  const files = openZip(opts.artifact)
  const connRaw = files.get('connection.json')
  const serverRaw = files.get('server.json')
  if (!connRaw && !serverRaw) {
    throw new Error('MCP 包缺少 connection.json 或 server.json')
  }
  const metaLabel = connRaw ? 'connection.json' : 'server.json'
  const metaRaw = (connRaw ?? serverRaw) as Uint8Array
  let conn: unknown
  try {
    const parsed = JSON.parse(Buffer.from(metaRaw).toString('utf8'))
    conn = connRaw ? parsed : connectionFromServerJson(parsed as Record<string, unknown>)
  } catch (e) {
    throw new Error(`${metaLabel} 解析失败: ${e instanceof Error ? e.message : String(e)}`)
  }
  if (typeof conn !== 'object' || conn === null || Array.isArray(conn)) {
    throw new Error(`${metaLabel} 顶层必须是 JSON 对象`)
  }
  const base: McpServerItem = { name: opts.name, source: `capability: ${opts.name}@${opts.version}` }
  const item =
    mode === 'platform'
      ? platformBridgeEntry(base)
      : mode === 'local'
        ? await localMcpEntry(dataDir, conn as Record<string, unknown>, base, files)
        : mcpEntryFromConnection(conn as Record<string, unknown>, opts.name, opts.version)
  // MCP 安全:本地拉起会执行本机命令,安装前须用户显式确认(展示完整 command/args)。
  if (item.command && !opts.confirmed) {
    throw new ConsentRequiredError({
      command: item.command,
      args: item.args ?? [],
      envKeys: Object.keys(item.env ?? {}),
      ...(item.cwd ? { cwd: item.cwd } : {})
    })
  }
  const configPath = mcpConfigPath(dataDir)
  const config = await loadMcpConfig(configPath)
  const next = (config.servers as unknown[]).filter((s) => {
    const nm = (s as { name?: unknown })?.name
    return typeof nm !== 'string' || nm !== opts.name
  })
  next.push(item)
  await saveMcpConfig(configPath, next)
  return item
}

// ---- agent ----

async function installAgent(dataDir: string, opts: InstallCapabilityOptions): Promise<void> {
  assertSafeCapabilityName(opts.name)
  const files = openZip(opts.artifact)
  const agentJson = files.get('agent.json')
  const prompt = files.get('PROMPT.md')
  if (!agentJson) throw new Error('智能体包缺少 agent.json')
  if (!prompt) throw new Error('智能体包缺少 PROMPT.md')
  const dir = join(agentsRoot(dataDir), opts.name)
  await mkdir(dir, { recursive: true })
  await writeFile(join(dir, 'agent.json'), agentJson)
  await writeFile(join(dir, 'PROMPT.md'), prompt)
  const team = files.get('TEAM.md')
  if (team) await writeFile(join(dir, 'TEAM.md'), team)
}

// ---- 卸载 ----

export interface RemoveDirResult {
  ok: boolean
  error?: string
}

export interface RemoveDirOptions {
  /** 总尝试次数（含首次），缺省 5 */
  attempts?: number
  /** 首次重试等待毫秒数，按 attempts 线性退避（delayMs * 次数），缺省 200 */
  delayMs?: number
  /** 测试注入：实际删除实现（缺省 rmSync recursive/force） */
  rmImpl?: (dir: string) => void
  /** 测试注入：等待实现（缺省 setTimeout Promise） */
  sleepImpl?: (ms: number) => void | Promise<void>
}

/** 可重试的删除失败错误码（Windows 文件占用/目录非空） */
const RETRYABLE_RM_CODES = new Set(['EPERM', 'EBUSY', 'ENOTEMPTY'])

const defaultSleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * 删除目录，遇文件占用（EPERM/EBUSY/ENOTEMPTY）按退避重试；
 * 目录本就不存在（ENOENT/ENOTFOUND）视为成功；最终仍失败不抛错，返回 { ok:false, error }。
 */
export async function removeDirWithRetry(
  dir: string,
  opts: RemoveDirOptions = {}
): Promise<RemoveDirResult> {
  const attempts = opts.attempts ?? 5
  const delayMs = opts.delayMs ?? 200
  const rm = opts.rmImpl ?? ((target: string) => rmSync(target, { recursive: true, force: true }))
  const sleep = opts.sleepImpl ?? defaultSleep
  let lastError = ''
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      rm(dir)
      return { ok: true }
    } catch (e) {
      const code = (e as NodeJS.ErrnoException)?.code ?? ''
      if (code === 'ENOENT' || code === 'ENOTFOUND') return { ok: true }
      lastError = e instanceof Error ? e.message : String(e)
      if (!RETRYABLE_RM_CODES.has(code) || attempt === attempts) break
      await sleep(delayMs * attempt)
    }
  }
  return { ok: false, error: lastError }
}

async function uninstallMcp(
  dataDir: string,
  name: string,
  removeDir: (dir: string) => Promise<RemoveDirResult> = removeDirWithRetry
): Promise<InstallResult> {
  // 本地安装解压出的实现文件目录(存在才清理;删除失败不阻断条目卸载,只在输出里提示原因)
  const localDir = join(mcpServersRoot(dataDir), name)
  const hadLocalFiles = existsSync(localDir)
  const removal: RemoveDirResult = hadLocalFiles ? await removeDir(localDir) : { ok: true }
  const localSuffix = hadLocalFiles ? '（含本地安装文件）' : ''
  const busyWarn = removal.ok
    ? ''
    : `（本地文件被占用未能删除：${localDir}，重启 dashboard 后可重试/手动清理）`
  const configPath = mcpConfigPath(dataDir)
  if (!existsSync(configPath)) {
    return { ok: true, output: `本地 MCP 配置不存在，无需卸载${busyWarn}` }
  }
  const config = await loadMcpConfig(configPath)
  const before = config.servers.length
  const next = config.servers.filter((s) => {
    const nm = (s as { name?: unknown })?.name
    return typeof nm !== 'string' || nm !== name
  })
  if (next.length === before) {
    return { ok: true, output: `本地 MCP 配置中未发现 ${name}${busyWarn}` }
  }
  await saveMcpConfig(configPath, next)
  return { ok: true, output: `已移除 MCP 配置条目 ${name}${localSuffix}${busyWarn}` }
}

// ---- 入口 ----

/**
 * remote（云端订阅即用）能力在 dashboard 的本地安装约束：
 * - skill/agent：不再本地安装；
 * - mcp：仅允许平台桥接（mode='platform'），mode 缺省或 local 均拒绝；
 * - tool：其“安装”是注册远程工具，放行。
 * runtime（market §3）优先：按维度独立生效（local=false → 拒本地安装；cloud=false → 拒云端托管），
 * 缺失维度/整体缺失严格回退现有 distribution 判断（灰度兼容，旧字段行为不变）。
 * 返回拒绝文案；允许安装返回 null。
 */
export function shouldRefuseRemoteInstall(
  cap: { type: MarketCapabilityType; distribution?: string; runtime?: MarketRuntime },
  mode?: McpInstallMode
): string | null {
  const rt = cap.runtime
  const hasRuntime = !!rt && (typeof rt.cloud === 'boolean' || typeof rt.local === 'boolean')
  if (hasRuntime) {
    const local = typeof rt.local === 'boolean' ? rt.local : cap.distribution !== 'remote'
    if (cap.type === 'skill' || cap.type === 'agent') {
      return local ? null : '云端能力无需安装，加入即可使用'
    }
    if (cap.type === 'mcp') {
      if (!local && mode !== 'platform') {
        return '云端能力无需本地安装，加入即可使用；请改用云端托管'
      }
      if (rt.cloud === false && mode === 'platform') {
        return '该能力仅支持本地安装，无法云端托管；请改用本地安装'
      }
      return null
    }
    return null
  }
  // runtime 缺失：回退现有 distribution 判断（行为不变）
  if (cap.distribution !== 'remote') return null
  if (cap.type === 'skill' || cap.type === 'agent') return '云端能力无需安装，加入即可使用'
  if (cap.type === 'mcp' && mode !== 'platform') {
    return '云端能力无需本地安装，加入即可使用；请改用云端托管'
  }
  return null
}

export async function installCapability(opts: InstallCapabilityOptions): Promise<InstallResult> {
  try {
    const { dataDir, type, name, version } = opts
    switch (type) {
      case 'skill': {
        await installSkill(dataDir, opts)
        return { ok: true, output: `技能 ${name}@${version} 已安装到本地技能目录` }
      }
      case 'mcp': {
        const item = await installMcp(dataDir, opts, opts.mode)
        if (opts.mode === 'platform') {
          return {
            ok: true,
            output:
              `MCP ${name}@${version} 已写入本地 MCP 配置（云端托管：经平台 MCP 网关，运行时注入地址与令牌）。` +
              '如需改为本地安装：先卸载，重新安装时选「本地安装」'
          }
        }
        if (opts.mode === 'local') {
          return {
            ok: true,
            output:
              `MCP ${name}@${version} 已写入本地 MCP 配置（本地安装：由本机直接拉起）。` +
              '如需改为云端托管：先卸载，重新安装时选「云端托管」'
          }
        }
        return item.kind === 'market-remote'
          ? { ok: true, output: `MCP ${name}@${version} 已写入本地 MCP 配置（该连接暂不支持本地拉起，标记 market-remote，待市场侧远程适配）` }
          : { ok: true, output: `MCP ${name}@${version} 已写入本地 MCP 配置` }
      }
      case 'agent': {
        await installAgent(dataDir, opts)
        return { ok: true, output: `智能体 ${name}@${version} 已安装为本地人设` }
      }
      case 'tool':
        return { ok: false, output: 'tool 类走远程适配，见市场页说明（本地不落盘）' }
      default:
        return { ok: false, output: `不支持的能力类型: ${String(type)}` }
    }
  } catch (e) {
    if (e instanceof ConsentRequiredError) {
      return { ok: false, output: e.message, consent: e.consent }
    }
    return { ok: false, output: e instanceof Error ? e.message : String(e) }
  }
}

export async function uninstallCapability(opts: UninstallCapabilityOptions): Promise<InstallResult> {
  try {
    const { dataDir, type, name } = opts
    assertSafeCapabilityName(name)
    switch (type) {
      case 'skill': {
        const dir = join(skillsRoot(dataDir), name)
        if (!existsSync(dir)) return { ok: true, output: `未发现本地技能 ${name}，无需卸载` }
        await rm(dir, { recursive: true, force: true })
        return { ok: true, output: `已卸载技能 ${name}` }
      }
      case 'agent': {
        const dir = join(agentsRoot(dataDir), name)
        if (!existsSync(dir)) return { ok: true, output: `未发现本地智能体 ${name}，无需卸载` }
        await rm(dir, { recursive: true, force: true })
        return { ok: true, output: `已卸载智能体 ${name}` }
      }
      case 'mcp':
        return await uninstallMcp(dataDir, name, opts.removeDirImpl)
      case 'tool':
        return { ok: false, output: 'tool 类为远程适配，请在市场「我的能力」移出（本地无安装可卸载）' }
      default:
        return { ok: false, output: `不支持的能力类型: ${String(type)}` }
    }
  } catch (e) {
    return { ok: false, output: e instanceof Error ? e.message : String(e) }
  }
}
