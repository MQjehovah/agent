import { randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import { basename, extname, isAbsolute, join as pjoin2 } from 'node:path'
import { join } from 'node:path'
import { app, BrowserWindow, dialog, ipcMain, nativeImage, shell, type IpcMainInvokeEvent, type WebContents } from 'electron'
import { getConfig } from '../store'
import { getIdentity, ensureRouterKey, freshAgentJwt, freshOidcAccessToken } from '../identity'
import {
  createSessionStore,
  parseModelIds,
  removeEphemeralSessions,
  resolveRunModel,
  type LocalSession,
  type SessionStore
} from './session'
import { createArchiveStore, normalizeArchiveKey, type ArchiveStore } from '../archive'
import { createTurnRegistry, type TurnClaim } from './turn-registry'
import {
  editAndResendLocalTurn,
  regenerateLocalTurn,
  startLocalTurn,
  type SessionActionDeps
} from './session-actions'
import {
  agentToExportSession,
  buildExportJson,
  buildExportMarkdown,
  localToExportSession,
  sanitizeExportFileName,
  type AgentHistoryMessage,
  type ExportSession
} from './export'
import { createPermissions, type PermissionGateway, type PermissionDecision, type PermissionRequest } from './permissions'
import { connectMcpServers, parseMcpConfig, type McpConnection, type McpGatewayTarget, type McpServerConfig } from './mcp'
import { createMarketClient, type MarketCapability, type MarketCapabilityType, type MarketClient } from './market'
import { installCapability, uninstallCapability, shouldRefuseRemoteInstall, assertSafeCapabilityName, type InstallResult, type McpInstallMode } from './installer'
import { createMarketTool, createMarketToolInvoker } from './market-tools'
import { loadMarketToolsFile, saveMarketToolsFile, type MarketToolRecord } from './market-registry'
import { loadDisabled, setDisabled } from './mcp-prefs'
import {
  loadConnectorEnv,
  saveConnectorEnv,
  type ConnectorEnvSnapshot
} from './mcp-env'
import { buildUserProfileLine } from './user-profile'
import {
  addAlwaysAllowed,
  clearAlwaysAllowed,
  listAlwaysAllowed,
  loadAlwaysAllowed,
  removeAlwaysAllowed
} from './permission-memory'
import { createRegistry, type Registry } from './registry'
import {
  createSearchToolsTool,
  isRemoteTool,
  normalizeToolSearchMode,
  progressiveHint,
  shouldUseProgressive,
  type ToolSearchEntry
} from './tool-search'
import { createRagSearcher } from './rag'
import { builtinTools, kbSearchTool } from './tools'
import { importAttachments, parseAttachImportPayload } from './attachment-import'
import { uploadAttachmentsToAgent } from './attachment-upload'
import { ALLOWED_EXTS, MAX_ATTACHMENT_BYTES, sanitizeAttachmentName } from './attachments'
import { resolveWithin } from './pathsafe'
import {
  listAgentArtifacts,
  listLocalArtifacts,
  readAgentArtifact,
  readLocalArtifact,
  type ArtifactContent,
  type ArtifactItem
} from './artifacts'
import {
  consumePickedPaths,
  prunePickedPaths,
  resolvePickedPaths,
  storePickedPaths,
  type PickedPathsStore
} from './attachment-tokens'
import { buildSystemPrompt, runAgentTurn } from './loop'
import { createSkillLoader, type SkillLoader } from './skills'
import type { AgentEvent, ChatMessage } from './types'

/**
 * agent 内核与 Electron IPC 的装配层：
 *   渲染层 invoke('localagent:*') → 主进程内核（session/permissions/mcp/loop/skills）。
 *   进程级单例在此模块内创建：session store、skills、permissions、registry、MCP 连接缓存。
 *   流式事件以 streamId 为键经 'localagent:event' 通道推送，风格与 upstream.ts 一致。
 */

/** 基础系统提示词：<workspace> 在 chat 时以会话工作区替换 */
const BASE_SYSTEM_PROMPT =
  '你是运行在员工本机的本地智能体，当前工作目录是 <workspace>。' +
  '你可以使用工具读取和写入工作区内的文件、执行终端命令、检索代码来完成任务。' +
  '执行命令前先评估影响，谨慎对待删除、覆盖、格式化等破坏性操作，拿不准时先向用户确认。' +
  '回答使用中文，简洁直接，引用文件时给出路径。'

/** 传给 loop 的上下文预算与轮数上限 */
const MAX_CONTEXT_CHARS = 96_000
const MAX_ROUNDS = 25

/** registry 工具名前缀：MCP 工具（mcp.ts mcpToolName）与市场远程工具（market-tools.ts）的公共约定 */
const MCP_TOOL_PREFIX = 'mcp__'
const MARKET_TOOL_PREFIX = 'market:'

// ---- 进程级单例（registerKernelIpc 内初始化，handler 只在注册后被调用） ----

let store: SessionStore
let skills: SkillLoader
let permissions: PermissionGateway
let archiveStore: ArchiveStore

/**
 * 会话轮次登记（同会话并发守卫 + streams/senders 账本）。
 * claim 是同步的，编排层（session-actions）保证它在任何破坏性写之前调用；
 * 窗口销毁时的业务清理（结算未决权限）通过回调注入。
 */
const turns = createTurnRegistry({
  onSenderDestroyed: (sessionId) => {
    permissions?.cancel(sessionId, true)
    pendingPermissionBySession.delete(sessionId)
  }
})

/** sessionId → 未决权限请求列表（已发给 UI、等待 respond 的请求） */
const pendingPermissionBySession = new Map<string, PermissionRequest[]>()

/**
 * 选文件令牌 → 用户真实选中的绝对路径（主进程内存态，渲染层不可见）。
 * 渲染层只能回传 token，无法自行指定源路径，从源头关闭「任意文件读取」原语。
 */
const pickedAttachmentPaths: PickedPathsStore = new Map()

/** 工具注册表进程内只构建一次：builtinTools 注册 + MCP 注册在 connect 内 */
let registry: Registry | null = null
/** MCP 连接缓存：首次 chat 时连接，后续复用 */
let mcpPromise: Promise<McpConnection[]> | null = null
const mcpHandles: McpConnection[] = []

/** 构建或复用注册表（内置工具 + kb_search）；首次构建时把上次会话安装的市场远程 tool 从清单读回注册 */
function ensureRegistry(): Registry {
  if (!registry) {
    const reg = createRegistry()
    for (const tool of builtinTools) reg.register(tool)
    // kb_search 依赖注入：RAG 直连实现带 OIDC access token（语义同 upstream 的 authFor('rag')），
    // 由 rag.ts 的可注入工厂构造，fetch/token/地址都可测；tools.ts 保持不碰 identity/upstream 的纯度
    reg.register(
      kbSearchTool(
        createRagSearcher({
          ragUrl: getConfig().ragUrl,
          getToken: () => getIdentity()?.oidc?.accessToken ?? null
        })
      )
    )
    // 渐进披露入口：search_tools 检索已注册远程工具并把命中项激活进当前会话（ctx.sessionId），
    // 写入会话 meta（会话内粘住）；loop 每轮重组工具列表，下一轮即可直接调用
    reg.register(
      createSearchToolsTool({
        listRemote: () => remoteToolEntries(reg),
        activate: (names, sessionId) => {
          const next = store.addActiveRemoteTools(sessionId, names)
          console.log(`[kernel] 工具搜索激活远程工具: ${names.join(', ')}（会话 ${sessionId} 共 ${next.length} 个）`)
          return next
        }
      })
    )
    // market 远程 tool 跨重启持久化：进程内注册随重启消失，按本地安装清单重建（已存在同名跳过）
    for (const rec of loadMarketToolsFile(gatewayDataDir())) {
      if (reg.get(MARKET_TOOL_PREFIX + rec.name)) continue
      reg.register(createMarketTool({ name: rec.name, description: rec.description }, invokeMarketRemoteTool))
    }
    registry = reg
  }
  return registry
}

/** registry 中的远程工具条目（渐进披露检索源；禁用/卸载的连接器不在其中，天然不计入） */
function remoteToolEntries(reg: Registry): ToolSearchEntry[] {
  return reg
    .list()
    .filter((t) => isRemoteTool(t.name))
    .map((t) => ({ name: t.name, description: t.description }))
}

/**
 * 连接器禁用/卸载后，把失效的远程工具从全部本地会话的激活集剔除，
 * 避免渐进模式把已不存在的工具名继续组装进请求（成本低，随之实现）。
 */
function pruneActivatedRemoteTools(isStale: (toolName: string) => boolean): void {
  let removed = 0
  for (const s of store.listSessions()) {
    const active = store.getActiveRemoteTools(s.id)
    const next = active.filter((name) => !isStale(name))
    if (next.length === active.length) continue
    store.setActiveRemoteTools(s.id, next)
    removed += active.length - next.length
  }
  if (removed > 0) console.log(`[kernel] 已从会话激活集剔除 ${removed} 个失效远程工具`)
}

/** 首次 chat 时连接 MCP 服务端（工具注册进 registry），后续复用同一 Promise */
function ensureMcp(reg: Registry): Promise<McpConnection[]> {
  if (!mcpPromise) {
    const dataDir = process.env.GATEWAY_DATA_DIR!
    // 用户禁用的连接器跳过（偏好缺省=启用；安装时无需写入，用户显式禁用才落盘）
    const disabled = loadDisabled(dataDir)
    mcpPromise = connectMcpServers({
      registry: reg,
      configPath: join(dataDir, 'localagent', 'mcp.json'),
      resolveGateway: resolveMarketGateway,
      resolveVars: resolveMcpVar,
      isDisabled: (name) => disabled.has(name),
      onStatus: (msg) => console.log(`[kernel] ${msg}`)
    })
      .then((handles) => {
        mcpHandles.push(...handles)
        return handles
      })
      .catch((err) => {
        console.warn(`[kernel] MCP 连接失败(忽略): ${err instanceof Error ? err.message : String(err)}`)
        return [] as McpConnection[]
      })
  }
  return mcpPromise
}

/**
 * 关闭某个连接器的连接句柄（卸载前调用）：从 mcpHandles 摘出该 server 的句柄并逐个关闭，
 * 再等一小段时间让 stdio 子进程退出、释放 cwd/文件句柄（否则 Windows 删除其目录会 EPERM）。
 * 与 invalidateMcpConnections 同序：连接在途时先等其结算，避免句柄在摘除后才注册回来。
 */
async function closeMcpHandlesFor(name: string): Promise<void> {
  await mcpPromise?.catch(() => {})
  const closing = mcpHandles.filter((h) => h.server === name)
  if (!closing.length) return
  for (let i = mcpHandles.length - 1; i >= 0; i -= 1) {
    if (mcpHandles[i].server === name) mcpHandles.splice(i, 1)
  }
  for (const h of closing) await h.close().catch(() => {})
  await new Promise((resolve) => setTimeout(resolve, 300))
}

/** 失效并重建 MCP 连接缓存：清 memoized Promise + 关闭旧句柄 + 摘除已注册的 MCP 工具。
 * 安装/卸载 mcp 能力后调用：进程级缓存不清，新装的 server 要到下次重启 chat 才会连上（Task3 评审结论），
 * 这里重置后，下一次 ensureMcp 会基于最新 mcp.json 重新连接并把工具重新注册进 registry。
 *
 * 串行化消除重连竞态：若有旧 connectMcpServers 仍在途（首次 chat 慢连、上次重连未结算），
 * 先 await 它结算完再清 memo/摘工具/关句柄——否则旧连接会在摘除后把工具注册回 registry
 * （残留工具指向已关闭客户端），且下次 ensureMcp 因重名跳过、新客户端永不生效。
 */
export async function invalidateMcpConnections(): Promise<void> {
  const pending = mcpPromise
  if (pending) {
    // 等 in-flight 结算：其注册先完整落地，随后的统一摘除才能把它清干净（memo 已折叠失败为 []，catch 纯兜底）
    await pending.catch(() => {})
  }
  mcpPromise = null
  // 先摘除现有全部 mcp__ 工具：连接重建后 connectMcpServers 会按最新 mcp.json 重新注册，
  // 若不摘，旧工具会留着指向已关闭客户端（且重连时因重名被跳过、永不刷新）。
  if (registry) {
    for (const tool of registry.list()) {
      if (tool.name.startsWith(MCP_TOOL_PREFIX)) registry.unregister(tool.name)
    }
  }
  // 结算后旧句柄已推进 mcpHandles（见 ensureMcp 的 .then），就地关闭
  const stale = mcpHandles.splice(0, mcpHandles.length)
  for (const h of stale) void h.close().catch(() => {})
}

// ---- 市场能力 IPC 支持：client 现读配置/身份、远程 tool 适配、本地清单与人设 ----

function gatewayDataDir(): string {
  return process.env.GATEWAY_DATA_DIR!
}

function localAgentsDir(): string {
  return join(gatewayDataDir(), 'localagent', 'agents')
}

/** 全部市场相关通道的身份闸：无 OIDC token 直接给中文提示，不发请求 */
function requireSsoLogin(): void {
  if (!getIdentity()?.oidc?.accessToken) throw new Error('请先完成企业 SSO 登录')
}

/**
 * market-gateway 连接参数解析：把能力名映射到平台 MCP 网关的 Streamable HTTP 端点。
 * 地址/身份每次现读（设置可运行期修改、token 会刷新）；无 SSO 登录时抛中文错误，
 * 由 connectMcpServers 捕获后跳过该条目（不影响其它 MCP server）。
 * 仅用于旧 kind:'market-gateway' 条目（读取兼容），新安装的平台桥接走占位符解析。
 */
async function resolveMarketGateway(name: string): Promise<McpGatewayTarget> {
  const token = getIdentity()?.oidc?.accessToken
  if (!token) throw new Error('请先完成企业 SSO 登录')
  const base = getConfig().marketUrl.replace(/\/$/, '')
  return {
    url: `${base}/api/mcp-gateway/relay/${encodeURIComponent(name)}/stream`,
    headers: { Authorization: `Bearer ${token}` }
  }
}

/**
 * MCP 配置占位符变量解析（仅注册这两个主进程变量，绝不读取 process.env，
 * 防 mcp.json 借 ${VAR} 占位符偷环境变量）：
 * - MARKET_URL：当前设置的市场地址（去尾斜杠）
 * - MARKET_TOKEN：当前企业身份的 access token（未登录返回 undefined，
 *   连接器会跳过该 server 并提示「请先完成企业 SSO 登录」）
 * 未注册变量返回 undefined。
 */
function resolveMcpVar(name: string): string | undefined {
  if (name === 'MARKET_URL') return getConfig().marketUrl.replace(/\/$/, '')
  if (name === 'MARKET_TOKEN') return getIdentity()?.oidc?.accessToken
  return undefined
}

/** market 客户端：地址每次现读 getConfig（设置可运行期修改），token 每次取当前身份 */
function createMarketClientForIpc(): MarketClient {
  return createMarketClient({
    marketUrl: getConfig().marketUrl,
    getToken: () => getIdentity()?.oidc?.accessToken ?? null
  })
}

/** 远程 tool 真实调用：同样每次现读地址与 token，注册时只记住名字不固化配置 */
async function invokeMarketRemoteTool(name: string, params: unknown): Promise<unknown> {
  const invoke = createMarketToolInvoker({
    marketUrl: getConfig().marketUrl,
    getToken: () => getIdentity()?.oidc?.accessToken ?? null
  })
  return invoke(name, params)
}

/** 注册/覆盖 market:<name> 远程 tool 到单例 registry；已存在同名先摘再注册（register 重名会抛错） */
function registerMarketRemoteTool(cap: MarketToolRecord): void {
  const reg = ensureRegistry()
  const full = MARKET_TOOL_PREFIX + cap.name
  reg.unregister(full)
  reg.register(createMarketTool({ name: cap.name, description: cap.description }, invokeMarketRemoteTool))
}

/** 从 registry 摘除 market:<name>；registry 尚未构建或工具不在时返回 false */
function unregisterMarketRemoteTool(name: string): boolean {
  return registry ? registry.unregister(MARKET_TOOL_PREFIX + name) : false
}

/** 安装清单中追加/覆盖一个市场远程 tool（先移除同名旧记录，避免重复），随后原子落盘 */
function persistMarketTool(dataDir: string, rec: MarketToolRecord): void {
  const next = loadMarketToolsFile(dataDir).filter((r) => r.name !== rec.name)
  next.push(rec)
  saveMarketToolsFile(dataDir, next)
}

interface LocalPersonaInfo {
  name: string
  description?: string
}

/** MCP 连接状态(给「+」菜单用): 配置了哪些、哪些已连上、哪些只能走远端、是否启用 */
interface McpStatusItem {
  name: string
  transport: 'stdio' | 'sse' | 'remote'
  status: 'connected' | 'idle'
  /** 本次进程是否已发起过连接尝试(false=尚未触发，首次对话时自动连接) */
  attempted: boolean
  /** 用户偏好：false=已禁用（记住），下一轮 chat 起跳过连接与工具注册 */
  enabled: boolean
}

function listMcpStatus(): McpStatusItem[] {
  const configPath = join(gatewayDataDir(), 'localagent', 'mcp.json')
  if (!existsSync(configPath)) return []
  let servers: McpServerConfig[] = []
  try {
    servers = parseMcpConfig(readFileSync(configPath, 'utf8'))
  } catch {
    return []
  }
  // market-remote 无 command/url，parseMcpConfig 不保留，这里再读一次原始 JSON 取标记；
  // market-gateway 由 parseMcpConfig 保留 kind，可直接判断
  const remoteNames = new Set<string>()
  try {
    const raw = JSON.parse(readFileSync(configPath, 'utf8')) as {
      servers?: Array<{ name?: string; kind?: string }>
    }
    for (const item of raw.servers ?? []) {
      if (item?.kind === 'market-remote' && item.name) remoteNames.add(String(item.name))
    }
  } catch {
    // 原始解析失败时按普通连接展示
  }
  const connected = new Set(mcpHandles.map((h) => h.server))
  const disabled = loadDisabled(gatewayDataDir())
  return servers.map((s) => {
    // 平台网关 = 旧 kind:'market-gateway' 条目，或新占位符形态（url 含 ${MARKET_URL}）；
    // market-remote 标记继续兜底（解析器不保留该 kind，保留原逻辑不回归）
    const platformBridge =
      s.kind === 'market-gateway' || remoteNames.has(s.name) ||
      (typeof s.url === 'string' && s.url.includes('${MARKET_URL}'))
    return {
      name: s.name,
      transport: platformBridge ? 'remote' : s.url ? 'sse' : 'stdio',
      status: connected.has(s.name) ? 'connected' : 'idle',
      attempted: mcpPromise !== null,
      enabled: !disabled.has(s.name)
    }
  })
}

/**
 * 「+」菜单逐项启用/禁用连接器：写偏好（只记禁用名单，缺省=启用）→ 失效连接缓存，
 * 下一轮 chat 的 ensureMcp 按最新偏好跳过/重连；合法名校验与 installer 同一套（防逃逸）。
 */
async function handleMcpSetEnabled(
  _event: IpcMainInvokeEvent,
  payload?: { name?: string; enabled?: boolean }
): Promise<{ ok: true; name: string; enabled: boolean }> {
  const name = typeof payload?.name === 'string' ? payload.name.trim() : ''
  if (!name) throw new Error('缺少连接器名称 name')
  assertSafeCapabilityName(name)
  if (typeof payload?.enabled !== 'boolean') throw new Error('缺少 enabled 参数（true/false）')
  setDisabled(gatewayDataDir(), name, !payload.enabled)
  await invalidateMcpConnections()
  // 禁用后该连接器的 mcp__<name>__ 工具不再注册：同步从各会话激活集剔除，避免残留失效工具名
  if (!payload.enabled) {
    pruneActivatedRemoteTools((toolName) => toolName.startsWith(`${MCP_TOOL_PREFIX}${name}__`))
  }
  return { ok: true, name, enabled: payload.enabled }
}

/** 连接器 env 名称参数校验（防路径/控制字符；与安装/偏好同一套） */
function requireConnectorName(payload?: { name?: string }): string {
  const name = typeof payload?.name === 'string' ? payload.name.trim() : ''
  if (!name) throw new Error('缺少连接器名称 name')
  assertSafeCapabilityName(name)
  return name
}

/** 「+」菜单：读本机连接器当前 env（autoVars 为内核托管变量，UI 只读） */
function handleMcpEnvGet(
  _event: IpcMainInvokeEvent,
  payload?: { name?: string }
): ConnectorEnvSnapshot {
  const name = requireConnectorName(payload)
  return loadConnectorEnv(gatewayDataDir(), name)
}

/** 「+」菜单：写回本机连接器 env（保留 auto 原值；空串删除；非法键拒绝）→ 连接缓存失效，下次 chat 生效 */
async function handleMcpEnvSet(
  _event: IpcMainInvokeEvent,
  payload?: { name?: string; env?: unknown }
): Promise<{ ok: true; name: string }> {
  const name = requireConnectorName(payload)
  const raw = payload?.env
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    throw new Error('缺少 env 参数（键值对象）')
  }
  const edited: Record<string, string> = {}
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    edited[key] = value === null || value === undefined ? '' : String(value)
  }
  saveConnectorEnv(gatewayDataDir(), name, edited)
  await invalidateMcpConnections()
  return { ok: true, name }
}

/** 扫 localagent/agents 下各子目录列人设；agent.json 存在则容错读 description，损坏/缺失不阻塞 */
function listLocalPersonas(): LocalPersonaInfo[] {
  const dir = localAgentsDir()
  if (!existsSync(dir)) return []
  const out: LocalPersonaInfo[] = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue
    const item: LocalPersonaInfo = { name: entry.name }
    const agentFile = join(dir, entry.name, 'agent.json')
    if (existsSync(agentFile)) {
      try {
        const parsed: unknown = JSON.parse(readFileSync(agentFile, 'utf8'))
        const desc =
          typeof parsed === 'object' && parsed !== null
            ? (parsed as Record<string, unknown>).description
            : undefined
        if (typeof desc === 'string' && desc.trim()) item.description = desc.trim()
      } catch {
        // agent.json 损坏仅该条描述缺失，仍保留名字
      }
    }
    out.push(item)
  }
  return out.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
}

/** 人设名会拼进文件路径，先做与能力名一致的防逃逸校验 */
function assertSafePersonaName(name: string): void {
  if (typeof name !== 'string' || name.length === 0 || name.length > 128 || name === '.' || name === '..') {
    throw new Error('非法人设名称')
  }
  if (/[\\/:*?"<>|\u0000-\u001f\u007f]/.test(name)) {
    throw new Error('非法人设名称（不能含路径分隔符、控制字符或 Windows 非法字符）')
  }
}

/** 读人设 PROMPT.md（新建本地会话选用市场 agent 人设时用）；缺失给清晰中文错误 */
function readPersona(name: string): { name: string; prompt: string } {
  assertSafePersonaName(name)
  const file = join(localAgentsDir(), name, 'PROMPT.md')
  let raw: string
  try {
    raw = readFileSync(file, 'utf8')
  } catch {
    throw new Error(`本地未安装人设「${name}」，请先在能力市场安装对应的 agent 能力`)
  }
  const prompt = raw.trim()
  if (!prompt) throw new Error(`人设「${name}」的 PROMPT.md 为空`)
  return { name, prompt }
}

function isMarketCapabilityType(v: unknown): v is MarketCapabilityType {
  return v === 'skill' || v === 'agent' || v === 'tool' || v === 'mcp'
}

/** 已安装清单条目：本地落盘（skill/agent/mcp）+ 注册表远程 tool */
interface InstalledCapabilityItem {
  type: MarketCapabilityType
  name: string
  version?: string
  description?: string
  /** mcp 条目当前模式：platform=平台桥接、local=本地安装；其它类型/无法判定时不带 */
  mode?: 'platform' | 'local'
}

/** 本地已安装能力清单：skill/agent 扫目录、mcp 扫 mcp.json 的 capability 来源条目、tool 以本地安装清单为准 */
function handleMarketListInstalled(): InstalledCapabilityItem[] {
  const dataDir = gatewayDataDir()
  const items: InstalledCapabilityItem[] = []
  // skill：与 skills loader 同一目录，复用其解析（list 每次重扫，安装即生效）
  for (const s of skills.listSkills()) {
    items.push({ type: 'skill', name: s.name, description: s.description })
  }
  // agent：agents 目录（即人设）
  for (const p of listLocalPersonas()) {
    const item: InstalledCapabilityItem = { type: 'agent', name: p.name }
    if (p.description) item.description = p.description
    items.push(item)
  }
  // mcp：mcp.json 里 source 形如 `capability: <name>@<version>` 的条目
  const mcpFile = join(dataDir, 'localagent', 'mcp.json')
  if (existsSync(mcpFile)) {
    try {
      const parsed: unknown = JSON.parse(readFileSync(mcpFile, 'utf8'))
      const servers =
        typeof parsed === 'object' && parsed !== null ? (parsed as { servers?: unknown }).servers : null
      if (Array.isArray(servers)) {
        for (const raw of servers) {
          const entry = raw as { name?: unknown; source?: unknown; kind?: unknown; command?: unknown; url?: unknown }
          const source = typeof entry.source === 'string' ? entry.source : ''
          if (!source.startsWith('capability:')) continue
          const name = typeof entry.name === 'string' ? entry.name : ''
          if (!name) continue
          const item: InstalledCapabilityItem = { type: 'mcp', name }
          const at = source.lastIndexOf('@')
          const version = at > 0 ? source.slice(at + 1).trim() : ''
          if (version) item.version = version
          // 当前模式：旧 kind:market-gateway 或新占位符 url（含 ${MARKET_URL}）=平台桥接；
          // command/url=本地安装；market-remote 等无法判定则不带
          if (entry.kind === 'market-gateway' || (typeof entry.url === 'string' && entry.url.includes('${MARKET_URL}'))) {
            item.mode = 'platform'
          } else if (typeof entry.command === 'string' || typeof entry.url === 'string') {
            item.mode = 'local'
          }
          items.push(item)
        }
      }
    } catch {
      // mcp.json 损坏只跳过该来源，不影响其余清单
    }
  }
  // tool：以本地安装清单为准（重启后 registry 尚未构建也能列全），
  // 再并入 registry 里多出的 market: 工具（旧会话内存态 / 清单缺失的兜底），按名去重
  const seenTool = new Set<string>()
  for (const rec of loadMarketToolsFile(dataDir)) {
    const item: InstalledCapabilityItem = { type: 'tool', name: rec.name }
    if (rec.version) item.version = rec.version
    if (rec.description) item.description = rec.description
    items.push(item)
    seenTool.add(rec.name)
  }
  for (const t of registry?.list() ?? []) {
    if (!t.name.startsWith(MARKET_TOOL_PREFIX)) continue
    const name = t.name.slice(MARKET_TOOL_PREFIX.length)
    if (seenTool.has(name)) continue
    items.push({ type: 'tool', name, description: t.description })
  }
  return items
}

// ---- 市场能力 IPC handlers ----

function handleMarketListMy(): Promise<MarketCapability[]> {
  requireSsoLogin()
  return createMarketClientForIpc().listMy('added')
}

async function handleMarketSubscribe(
  _event: IpcMainInvokeEvent,
  payload?: { capabilityId?: string }
): Promise<{ ok: boolean; output: string }> {
  requireSsoLogin()
  const capabilityId = typeof payload?.capabilityId === 'string' ? payload.capabilityId.trim() : ''
  if (!capabilityId) throw new Error('缺少能力 id (capabilityId)')
  await createMarketClientForIpc().subscribe(capabilityId)
  return { ok: true, output: '已加入我的能力' }
}

async function handleMarketUnsubscribe(
  _event: IpcMainInvokeEvent,
  payload?: { capabilityId?: string }
): Promise<{ ok: boolean; output: string }> {
  requireSsoLogin()
  const capabilityId = typeof payload?.capabilityId === 'string' ? payload.capabilityId.trim() : ''
  if (!capabilityId) throw new Error('缺少能力 id (capabilityId)')
  await createMarketClientForIpc().unsubscribe(capabilityId)
  return { ok: true, output: '已从我的能力移除' }
}

/**
 * 安装：确保 added（subscribe 幂等）→ 按 type 落地。
 *   skill/agent/mcp：下载能力包 → installer 解包落盘；mcp 成功后失效 MCP 缓存，下次 chat 连新 server。
 *   tool：不下载代码，注册 market:<name> 远程适配工具（云端 runtime 执行）并把记录写入本地安装清单，重启后恢复。
 *   mcp 的 payload.mode 指定安装模式（platform=平台桥接 / local=本地安装），未传保持自动判定。
 */
async function handleMarketInstall(
  _event: IpcMainInvokeEvent,
  payload?: { name?: string; version?: string; mode?: string; confirmed?: boolean }
): Promise<InstallResult> {
  requireSsoLogin()
  const name = typeof payload?.name === 'string' ? payload.name.trim() : ''
  if (!name) throw new Error('缺少能力名称 name')
  const version =
    typeof payload?.version === 'string' && payload.version.trim() ? payload.version.trim() : undefined
  const modeRaw = payload?.mode
  if (modeRaw !== undefined && modeRaw !== 'platform' && modeRaw !== 'local') {
    throw new Error(`不支持的安装模式: ${String(modeRaw)}`)
  }
  const mode = modeRaw as McpInstallMode | undefined
  const client = createMarketClientForIpc()
  // 安装对象限定在「我的能力」内（含我创建的）：先按名定位，拿到 id/type/description/当前版本。
  // 未订阅的新能力拿不到 id，无法 subscribe——引导先订阅再安装，符合订阅鉴权的权限模型。
  const mine = await client.listMy('all')
  const cap = mine.find((c) => c.name === name)
  if (!cap) throw new Error(`市场「我的能力」中未找到 ${name}，请先在市场加入后再安装`)
  // remote 云端能力订阅即用：技能/助手拒绝本地安装；MCP 仅平台桥接（mode 缺省同 local）
  const refusal = shouldRefuseRemoteInstall(cap, mode)
  if (refusal) return { ok: false, output: refusal }
  // 确保 added：后端对已加入幂等返回 200；其余失败忽略，交给下载/安装步骤暴露真实问题
  await client.subscribe(cap.id).catch(() => {})

  if (cap.type === 'tool') {
    const rec: MarketToolRecord = { name: cap.name, version: cap.version, description: cap.description }
    registerMarketRemoteTool(rec)
    persistMarketTool(gatewayDataDir(), rec)
    return { ok: true, output: `市场工具 ${cap.name} 已注册为远程工具 ${MARKET_TOOL_PREFIX}${cap.name}（云端执行，重启后自动恢复）` }
  }

  const artifact = await client.download(name, version)
  const res = await installCapability({
    dataDir: gatewayDataDir(),
    type: cap.type,
    name,
    version: version ?? cap.version,
    artifact,
    description: cap.description,
    mode: cap.type === 'mcp' ? mode : undefined,
    confirmed: payload?.confirmed === true
  })
  if (!res.ok) {
    // 本地执行命令需确认: 原样返回命令预览, 由渲染层确认后带 confirmed 重试(未写配置)
    if (res.consent) return res
    throw new Error(res.output)
  }
  if (cap.type === 'mcp') await invalidateMcpConnections()
  return res
}

/** 卸载：tool 摘 registry 的 market: 工具；skill/agent/mcp 走 installer；mcp 卸载后失效 MCP 缓存 */
async function handleMarketUninstall(
  _event: IpcMainInvokeEvent,
  payload?: { type?: string; name?: string }
): Promise<InstallResult> {
  requireSsoLogin()
  const type = payload?.type
  if (!isMarketCapabilityType(type)) throw new Error(`不支持的能力类型: ${String(type)}`)
  const name = typeof payload?.name === 'string' ? payload.name.trim() : ''
  if (!name) throw new Error('缺少能力名称 name')
  if (type === 'tool') {
    const removed = unregisterMarketRemoteTool(name)
    // 无论 registry 是否已构建，都从本地安装清单移除，保证重启后不再恢复
    const list = loadMarketToolsFile(gatewayDataDir())
    const next = list.filter((r) => r.name !== name)
    const fromManifest = next.length !== list.length
    if (fromManifest) saveMarketToolsFile(gatewayDataDir(), next)
    // 同步从各会话激活集剔除该远程工具（渐进披露）
    pruneActivatedRemoteTools((toolName) => toolName === MARKET_TOOL_PREFIX + name)
    return {
      ok: true,
      output: removed || fromManifest ? `已卸载市场工具 ${name}` : `未发现已安装的市场工具 ${name}`
    }
  }
  if (type === 'mcp') {
    // 先断开该连接并等子进程退出（stdio cwd 指向包目录，占用会让删除报 EPERM）
    await closeMcpHandlesFor(name)
  }
  const res = await uninstallCapability({ dataDir: gatewayDataDir(), type, name })
  if (type === 'mcp' && !res.ok) {
    // 目录已关连接但卸载未完成（如配置不可写）：仍需失效连接缓存，避免残留指向已关闭客户端的句柄
    await invalidateMcpConnections()
  }
  if (!res.ok) throw new Error(res.output)
  if (type === 'mcp') {
    // 顺带清理禁用记录：重新安装按「安装默认启用」语义生效
    setDisabled(gatewayDataDir(), name, false)
    await invalidateMcpConnections()
    // 卸载后该连接器的工具不再注册：同步从各会话激活集剔除
    pruneActivatedRemoteTools((toolName) => toolName.startsWith(`${MCP_TOOL_PREFIX}${name}__`))
  }
  return res
}

// ---- IPC handlers ----

async function handleWorkspacePick(event: IpcMainInvokeEvent): Promise<{ canceled: boolean; path?: string }> {
  const win = BrowserWindow.fromWebContents(event.sender)
  const options = { properties: ['openDirectory', 'createDirectory'] as Array<'openDirectory' | 'createDirectory'> }
  const result = win ? await dialog.showOpenDialog(win, options) : await dialog.showOpenDialog(options)
  if (result.canceled || result.filePaths.length === 0) return { canceled: true }
  return { canceled: false, path: result.filePaths[0] }
}

/**
 * 选文件（多选）：主进程登记选中路径并只回传一次性 token，绝不把绝对路径交给渲染层。
 * 文件安全校验在 attach:import 内按 token 解析出的路径做。
 */
async function handleFilePick(event: IpcMainInvokeEvent): Promise<{ canceled: boolean; token?: string }> {
  const win = BrowserWindow.fromWebContents(event.sender)
  const options = { properties: ['openFile', 'multiSelections'] as Array<'openFile' | 'multiSelections'> }
  const result = win ? await dialog.showOpenDialog(win, options) : await dialog.showOpenDialog(options)
  if (result.canceled || result.filePaths.length === 0) return { canceled: true }
  prunePickedPaths(pickedAttachmentPaths)
  const token = randomBytes(16).toString('hex')
  storePickedPaths(pickedAttachmentPaths, token, result.filePaths)
  return { canceled: false, token }
}

/** 图片附件缩略图(96px data URL): 仅常见图片且 4MB 内, 失败返回 undefined */
const THUMB_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp'])
const THUMB_MAX_BYTES = 4 * 1024 * 1024

async function thumbForFile(filePath: string): Promise<string | undefined> {
  try {
    if (!THUMB_EXTS.has(extname(filePath).toLowerCase())) return undefined
    if (statSync(filePath).size > THUMB_MAX_BYTES) return undefined
    const img = await nativeImage.createThumbnailFromPath(filePath, { width: 96, height: 96 })
    return img.isEmpty() ? undefined : img.toDataURL()
  } catch {
    return undefined
  }
}

/**
 * 粘贴板图片落盘：把渲染层从剪贴板拿到的图片字节写成临时文件并发放令牌，
 * 后续走与"选择文件"完全相同的导入/上传流程(渲染层始终拿不到任意路径)。
 */async function handleFilePaste(
  _event: IpcMainInvokeEvent,
  payload?: { name?: string; bytes?: Uint8Array | ArrayBuffer }
): Promise<{ token: string; name: string }> {
  const raw = payload?.bytes
  if (!raw) throw new Error('剪贴板里没有图片内容')
  const buf = Buffer.from(raw instanceof ArrayBuffer ? new Uint8Array(raw) : raw)
  if (buf.byteLength === 0) throw new Error('剪贴板图片为空')
  if (buf.byteLength > MAX_ATTACHMENT_BYTES) throw new Error('图片超过 20MB 上限')

  const requested = sanitizeAttachmentName(String(payload?.name ?? 'pasted.png'))
  const ext = (requested.match(/\.[a-z0-9]+$/i) ?? [''])[0].toLowerCase()
  const name = ALLOWED_EXTS.has(ext) ? requested : `pasted-${Date.now()}.png`

  const dir = join(app.getPath('temp'), 'dashboard-paste')
  mkdirSync(dir, { recursive: true })
  const file = join(dir, `${Date.now()}-${name}`)
  writeFileSync(file, buf)

  prunePickedPaths(pickedAttachmentPaths)
  const token = randomBytes(16).toString('hex')
  storePickedPaths(pickedAttachmentPaths, token, [file])
  return { token, name }
}

/**
 * 把选中的文件复制进会话工作区 `.attachments/`。
 *
 * 安全边界：
 *   - 入参只认 `{ sessionId, token }`，渲染层传来的 paths 一律忽略；
 *   - 源路径由主进程令牌解析，渲染层无法凭空指定要复制的文件；
 *   - 工作区一律由主进程按 sessionId 读会话 meta 得到，绝不信任渲染层传来的路径；
 *   - 目标名经 attachmentTargetName 清洗与控制字符剥离，再用 resolveWithin 二次兜底，
 *     保证任何文件名都无法逃出 `<workspace>/.attachments/`；
 *   - 单个文件失败只记入 skipped，不中断整批。
 */
async function handleAttachImport(
  _event: IpcMainInvokeEvent,
  payload?: unknown
): Promise<{ imported: { relPath: string; name: string; thumb?: string }[]; skipped: { path: string; reason: string }[] }> {
  const { sessionId, token } = parseAttachImportPayload(payload)
  const session = store.getSession(sessionId)
  if (!session) throw new Error('会话不存在')
  const workspace = session.workspace
  if (typeof workspace !== 'string' || !isAbsolute(workspace)) throw new Error('会话工作区无效')

  const paths = resolvePickedPaths(pickedAttachmentPaths, token)
  const attachmentsDir = join(workspace, '.attachments')
  const res = importAttachments(attachmentsDir, paths)
  // 图片附件带 96px 缩略图(data URL), 便于待发 chip 预览
  const imported = await Promise.all(
    res.imported.map(async (item) => ({
      ...item,
      thumb: await thumbForFile(join(attachmentsDir, item.relPath))
    }))
  )
  // 导入成功即消费令牌：同一个 token 不能二次导入
  consumePickedPaths(pickedAttachmentPaths, token)
  return { imported, skipped: res.skipped }
}

/**
 * 在线(agent)附件上传：把已授权路径 POST 到 agent `/api/workspace/upload`，
 * 落到「我的工作区」uploads/，返回的 relPath 由 agent 的 file 工具读取。
 */
async function handleAgentAttachUpload(
  _event: IpcMainInvokeEvent,
  payload?: unknown
): Promise<{ imported: { relPath: string; name: string; thumb?: string }[]; skipped: { path: string; reason: string }[] }> {
  const token = typeof (payload as { token?: unknown } | undefined)?.token === 'string'
    ? String((payload as { token?: string }).token).trim()
    : ''
  if (!token) throw new Error('缺少附件令牌，请重新选择文件')
  const identity = getIdentity()
  if (!identity?.agentJwt) throw new Error('请先完成企业 SSO 登录')

  const paths = resolvePickedPaths(pickedAttachmentPaths, token)
  const thumbs = new Map<string, string>()
  for (const p of paths) {
    const thumb = await thumbForFile(p)
    if (thumb) thumbs.set(basename(p), thumb)
  }
  const res = await uploadAttachmentsToAgent(paths, {
    baseUrl: getConfig().agentUrl,
    token: identity.agentJwt
  })
  consumePickedPaths(pickedAttachmentPaths, token)
  return {
    imported: res.imported.map((item) => ({ ...item, thumb: thumbs.get(item.name) })),
    skipped: res.skipped
  }
}

/** 产物面板: 本地会话工作区(离线)
 *
 * 在线产物在服务端(agent「我的工作区」), 只能预览不能在本机定位/打开;
 * 离线产物在本地工作区, 可定位/用系统程序打开。
 */
// ---- 工作空间清单(本地模式: 搜索/新建/最近使用) ----

interface WorkspaceItem {
  path: string
  name: string
  lastUsedAt: number
}

const WORKSPACE_KEEP = 50
const WORKSPACE_ROOT_NAME = 'AI 工作空间'

function workspacesFilePath(): string {
  return pjoin2(gatewayDataDir(), 'localagent', 'workspaces.json')
}

function loadWorkspaces(): WorkspaceItem[] {
  try {
    const raw = JSON.parse(readFileSync(workspacesFilePath(), 'utf8')) as unknown
    if (!Array.isArray(raw)) return []
    return (raw as Array<Record<string, unknown>>)
      .filter((it) => typeof it?.path === 'string' && it.path)
      .map((it) => ({
        path: String(it.path),
        name: typeof it.name === 'string' && it.name ? it.name : basename(String(it.path)),
        lastUsedAt: Number(it.lastUsedAt) || 0
      }))
      .sort((a, b) => b.lastUsedAt - a.lastUsedAt)
  } catch {
    return []
  }
}

function saveWorkspaces(items: WorkspaceItem[]): void {
  try {
    mkdirSync(pjoin2(gatewayDataDir(), 'localagent'), { recursive: true })
    writeFileSync(workspacesFilePath(), `${JSON.stringify(items.slice(0, WORKSPACE_KEEP), null, 2)}\n`, 'utf8')
  } catch (err) {
    console.warn('[kernel] 工作空间清单写入失败:', (err as Error).message)
  }
}

/** 记录一次使用(去重, 最近使用在前) */
function touchWorkspace(workspace: string): void {
  const trimmed = workspace.trim()
  if (!trimmed) return
  const key = process.platform === 'win32' ? trimmed.toLowerCase() : trimmed
  const items = loadWorkspaces().filter(
    (it) => (process.platform === 'win32' ? it.path.toLowerCase() : it.path) !== key
  )
  items.unshift({ path: trimmed, name: basename(trimmed) || trimmed, lastUsedAt: Date.now() })
  saveWorkspaces(items)
}

/** 在用户目录下新建工作空间: <home>/AI 工作空间/<名字> */
function createWorkspace(name: string): WorkspaceItem {
  const cleaned = String(name || '')
    .replace(/[\\/:*?"<>|\u0000-\u001f\u007f]/g, '')
    .replace(/\.\./g, '')
    .trim()
  const safe = cleaned.slice(0, 60) || `工作空间-${Date.now()}`
  const dir = pjoin2(app.getPath('home'), WORKSPACE_ROOT_NAME, safe)
  mkdirSync(dir, { recursive: true })
  touchWorkspace(dir)
  return { path: dir, name: basename(dir), lastUsedAt: Date.now() }
}

function localWorkspaceFor(sessionId: string): string {
  const session = store.getSession(sessionId)
  const workspace = session?.workspace
  if (typeof workspace !== 'string' || !isAbsolute(workspace)) throw new Error('会话工作区无效')
  return workspace
}

async function handleArtifactList(
  _event: IpcMainInvokeEvent,
  payload?: { mode?: string; sessionId?: string }
): Promise<ArtifactItem[]> {
  if (payload?.mode === 'local') {
    return listLocalArtifacts(localWorkspaceFor(String(payload?.sessionId ?? '')))
  }
  return listAgentArtifacts()
}

async function handleArtifactRead(
  _event: IpcMainInvokeEvent,
  payload?: { mode?: string; sessionId?: string; relPath?: string }
): Promise<ArtifactContent> {
  const relPath = String(payload?.relPath ?? '')
  if (!relPath) throw new Error('缺少文件路径')
  if (payload?.mode === 'local') {
    return readLocalArtifact(localWorkspaceFor(String(payload?.sessionId ?? '')), relPath)
  }
  return readAgentArtifact(relPath)
}

/** 在系统文件管理器中定位产物(仅离线) */
function handleArtifactReveal(
  _event: IpcMainInvokeEvent,
  payload?: { sessionId?: string; relPath?: string }
): void {
  const workspace = localWorkspaceFor(String(payload?.sessionId ?? ''))
  shell.showItemInFolder(resolveWithin(workspace, String(payload?.relPath ?? '')))
}

/** 用系统默认程序打开产物(仅离线) */
async function handleArtifactOpen(
  _event: IpcMainInvokeEvent,
  payload?: { sessionId?: string; relPath?: string }
): Promise<string> {
  const workspace = localWorkspaceFor(String(payload?.sessionId ?? ''))
  return shell.openPath(resolveWithin(workspace, String(payload?.relPath ?? '')))
}

function handleSessionCreate(
  _event: IpcMainInvokeEvent,
  payload?: {
    model?: string
    workspace?: string
    title?: string
    systemPrompt?: string
    personaName?: string
    /** 临时会话(仅本地): 退出/启动时清理, UI 有「临时」徽标 */
    ephemeral?: boolean
  }
): unknown {
  const workspace = payload?.workspace
  if (typeof workspace !== 'string' || !isAbsolute(workspace)) {
    throw new Error('workspace 必须是绝对路径')
  }
  // 默认工作区可能尚未存在,创建会话时自动落目录
  try {
    mkdirSync(workspace, { recursive: true })
  } catch (err) {
    throw new Error(`创建工作目录失败: ${(err as Error).message}`)
  }
  touchWorkspace(workspace)
  const model =
    typeof payload?.model === 'string' && payload.model.trim() ? payload.model.trim() : getConfig().localModel
  // 可选人设：personaName 指向本地 agents/<name>，读 PROMPT.md 组装；不带则行为与旧版一致
  const personaName = typeof payload?.personaName === 'string' ? payload.personaName.trim() : ''
  const persona = personaName ? readPersona(personaName) : undefined
  return store.createSession({
    model,
    workspace,
    title: payload?.title,
    systemPrompt: payload?.systemPrompt,
    persona,
    ephemeral: payload?.ephemeral === true
  })
}

/** 网关模型清单缓存: 5 分钟内复用; 拉取失败不写缓存(下次重试) */
let modelListCache: { at: number; models: string[] } | null = null

/**
 * 拉取网关可用模型清单(供输入区「模型」下拉)。
 * 失败时回退为已知模型(含默认模型)并带 error 说明, 不阻断会话;
 * 凭据经 ensureRouterKey 自愈(缺失时用 SSO 凭据重换)。
 */
async function handleModelsList(): Promise<{ models: string[]; defaultModel: string; error?: string }> {
  const cfg = getConfig()
  const defaultModel = String(cfg.localModel ?? '').trim()
  const now = Date.now()
  if (modelListCache && now - modelListCache.at < 5 * 60_000) {
    return { models: modelListCache.models, defaultModel }
  }
  try {
    const key = await ensureRouterKey()
    const base = cfg.routerUrl.replace(/\/+$/, '')
    const res = await fetch(`${base}/v1/models`, { headers: { authorization: `Bearer ${key}` } })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const models = parseModelIds(await res.json())
    if (!models.length) throw new Error('网关未返回可用模型')
    modelListCache = { at: now, models }
    return { models, defaultModel }
  } catch (err) {
    const cached = modelListCache?.models ?? []
    const models = !defaultModel || cached.includes(defaultModel) ? cached : [defaultModel, ...cached]
    return { models, defaultModel, error: (err as Error).message }
  }
}

/**
 * 本地会话单轮流式执行（claim 已由编排层在破坏性写之前同步取得）：
 *   身份与 router key 准备 → 系统提示词 → loop 事件流；抛错时由编排层 release claim。
 */
async function executeLocalTurn(
  claim: TurnClaim,
  session: LocalSession,
  userMessage: string,
  options: { history: ChatMessage[]; skipPersistUserMessage?: boolean }
): Promise<{ streamId: string }> {
  const { sender, streamId, controller, onSenderDestroyed } = claim
  const identity = getIdentity()
  if (!identity) throw new Error('请先完成企业 SSO 登录')
  // key 缺失时用已存 SSO 凭据自动重试交换(自愈),仍失败才报错(编排层负责 release claim)
  const routerKey = await ensureRouterKey()

  const reg = ensureRegistry()
  // MCP 工具注册进 registry 后再开跑（首次 chat 时才真正连接）
  // 连接前先续期 SSO access_token(仅剩 <60s 才真刷新);令牌轮换则失效旧连接,
  // 避免平台桥接拿着过期 MARKET_TOKEN 收到 401「登录状态无效或已过期」
  const tokenBefore = getIdentity()?.oidc?.accessToken ?? ''
  await freshOidcAccessToken()
  const tokenAfter = getIdentity()?.oidc?.accessToken ?? ''
  if (tokenBefore && tokenAfter && tokenBefore !== tokenAfter) {
    await invalidateMcpConnections()
  }
  await ensureMcp(reg)

  const emit = (e: AgentEvent): void => {
    if (!sender.isDestroyed()) {
      sender.send('localagent:event', { streamId, ...e })
    }
  }

  // 系统提示词拼装：persona.prompt 优先作前缀 → 基础本地智能体提示 → 会话自定义 systemPrompt（如存在），
  // 最后由 buildSystemPrompt 接技能清单 addendum。不带 persona/systemPrompt 时与旧行为一致。
  const systemParts: string[] = []
  if (session.persona?.prompt) systemParts.push(session.persona.prompt)
  systemParts.push(BASE_SYSTEM_PROMPT.replace('<workspace>', session.workspace))
  if (session.systemPrompt?.trim()) systemParts.push(session.systemPrompt)
  // 渐进披露：每轮由 loop 现算是否只发内置 + 已激活远程工具；开启时在系统提示追加「先搜索再调用」说明
  const toolSearch = {
    progressive: () =>
      shouldUseProgressive(
        normalizeToolSearchMode(getConfig().localAgentToolSearch),
        remoteToolEntries(reg).length
      ),
    activeNames: () => store.getActiveRemoteTools(session.id)
  }
  if (toolSearch.progressive()) systemParts.push(progressiveHint(toolSearch.activeNames()))
  // 当前用户画像放最前：persona/BASE 之前可见；无身份信息则不改动既有拼装
  const userProfileLine = buildUserProfileLine(identity.user)
  if (userProfileLine) systemParts.unshift(userProfileLine)
  const systemPrompt = buildSystemPrompt(systemParts.join('\n\n'), skills.systemPromptAddendum())

  // fire and forget：loop 内部已把一切异常折叠为 error 事件，这里 .catch 兜底
  void runAgentTurn(
    {
      apiKey: routerKey,
      baseUrl: getConfig().routerUrl.replace(/\/+$/, ''),
      registry: reg,
      permissions,
      systemPrompt,
      toolSearch,
      maxRounds: MAX_ROUNDS,
      persist: (msg) => store.appendMessage(session.id, msg)
    },
    {
      sessionId: session.id,
      workspace: session.workspace,
      // 模型: 会话级优先(用户可在输入区切换), 缺省回退系统设置的默认模型
      model: resolveRunModel(session.model, getConfig().localModel),
      history: options.history,
      userMessage,
      skipPersistUserMessage: options.skipPersistUserMessage,
      emit,
      signal: controller.signal
    }
  )
    .catch((err) => {
      emit({ type: 'error', message: `agent 循环异常: ${err instanceof Error ? err.message : String(err)}` })
    })
    .finally(() => {
      // sender 已销毁时监听器随对象消亡，无需也无法移除；正常结束则摘除以免累积
      if (!sender.isDestroyed()) sender.removeListener('destroyed', onSenderDestroyed)
      // 流结束结算: 删流; 会话无活跃流时清 sender 与未决权限(与窗口销毁清理同约定)
      turns.settle(streamId, session.id, (sid) => pendingPermissionBySession.delete(sid))
    })

  return { streamId }
}

/** 编排层依赖: 借当前窗口的 sender 完成同步 claim, 破坏性写前置守卫 */
function actionDeps(sender: WebContents): SessionActionDeps<TurnClaim> {
  return {
    store,
    claim: (sessionId) => turns.claim(sessionId, sender),
    execute: executeLocalTurn,
    maxContextChars: MAX_CONTEXT_CHARS
  }
}

async function handleChat(
  event: IpcMainInvokeEvent,
  payload: { sessionId?: string; message?: string }
): Promise<{ streamId: string }> {
  // 守卫(claim)在编排层内先于 loop 落盘用户消息执行
  return startLocalTurn(actionDeps(event.sender), payload?.sessionId ?? '', payload?.message ?? '')
}

/**
 * 本地会话上下文用量(E): 与 buildContext 完全同口径估算, 供输入区徽标与 tooltip。
 * 模型名同 resolveRunModel(会话级 > 系统默认), 只读展示。
 */
function handleContextUsage(
  _event: IpcMainInvokeEvent,
  payload?: { sessionId?: string }
): { used: number; budget: number; messages: number; total: number; model: string } {
  const session = store.getSession(String(payload?.sessionId ?? ''))
  if (!session) throw new Error('会话不存在')
  const est = store.estimateContext(session.id, MAX_CONTEXT_CHARS)
  return {
    used: est.used,
    budget: MAX_CONTEXT_CHARS,
    messages: est.kept,
    total: est.total,
    model: resolveRunModel(session.model, getConfig().localModel)
  }
}

/** 重新生成(C2, 仅本地): 守卫 → 截断到最后一条用户消息(含) → 复用该消息重跑 */
async function handleRegenerate(
  event: IpcMainInvokeEvent,
  payload?: { sessionId?: string }
): Promise<{ streamId: string }> {
  return regenerateLocalTurn(actionDeps(event.sender), String(payload?.sessionId ?? ''))
}

/** 编辑并重发(C3, 仅本地): index 是「第几条用户消息」(不能用 UI 数组下标, 存储里夹着 tool 消息) */
async function handleEditAndResend(
  event: IpcMainInvokeEvent,
  payload?: { sessionId?: string; index?: number; text?: string }
): Promise<{ streamId: string }> {
  return editAndResendLocalTurn(
    actionDeps(event.sender),
    String(payload?.sessionId ?? ''),
    payload?.index,
    String(payload?.text ?? '')
  )
}

// ---- 会话归档(D1)与会话导出(D2) ----

/** agent 会话接口 GET(带用户 agent JWT), 错误解析出可读原因 */
async function agentApiGet<T>(path: string): Promise<T> {
  const jwt = await freshAgentJwt()
  if (!jwt) throw new Error('请先完成企业 SSO 登录')
  const base = getConfig().agentUrl.replace(/\/+$/, '')
  const res = await fetch(`${base}${path}`, { headers: { authorization: `Bearer ${jwt}` } })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    let reason = `agent 接口失败(HTTP ${res.status})`
    try {
      const parsed = JSON.parse(text) as { error?: string }
      if (parsed?.error) reason = String(parsed.error)
    } catch {
      // 非 JSON 响应保留默认原因
    }
    throw new Error(reason)
  }
  return (await res.json()) as T
}

/**
 * 在线会话导出数据: 标题优先取列表命中项, 未命中(如 200 条上限外)回退渲染层传入的已知标题,
 * 再回退「会话 <id>」; 消息走既有 messages 端点。
 */
async function fetchAgentExportSession(sessionId: string, titleHint: string): Promise<ExportSession> {
  const historyRes = await agentApiGet<{ sessions?: Array<Record<string, unknown>> }>(
    '/api/agent/sessions/history?limit=200'
  )
  const hit = (historyRes.sessions ?? []).find((s) => String(s.id) === sessionId)
  const messagesRes = await agentApiGet<{ messages?: AgentHistoryMessage[] }>(
    `/api/agent/sessions/messages?session_id=${encodeURIComponent(sessionId)}`
  )
  const title = String(hit?.title ?? '').trim() || titleHint
  return agentToExportSession(
    {
      id: sessionId,
      title,
      first_accessed: hit?.first_accessed,
      last_accessed: hit?.last_accessed,
      created_at: hit?.created_at
    },
    messagesRes.messages ?? []
  )
}

/** 解析会话键: `local:<id>` / `agent:<sessionId>`(归档与导出共用同一编码) */
function parseSessionKey(key: unknown): { mode: string; id: string } {
  const normalized = normalizeArchiveKey(key)
  if (!normalized) throw new Error('会话标识无效')
  const sep = normalized.indexOf(':')
  return { mode: normalized.slice(0, sep), id: normalized.slice(sep + 1) }
}

/** 读导出数据源: 本地会话直接读存储(临时会话拒绝), 在线会话拉 agent 接口 */
async function loadExportSession(key: unknown, titleHint: string): Promise<ExportSession> {
  const { mode, id } = parseSessionKey(key)
  if (mode === 'local') {
    const meta = store.getSession(id)
    if (!meta) throw new Error('本地会话不存在')
    if (meta.ephemeral) throw new Error('临时会话不支持导出')
    return localToExportSession(meta, store.getMessages(id))
  }
  if (mode === 'agent') return fetchAgentExportSession(id, titleHint)
  throw new Error(`不支持的会话类型: ${mode}`)
}

/** 会话导出: 组装内容 → 系统保存对话框 → 写文件; 取消返回 canceled */
async function handleSessionExport(
  event: IpcMainInvokeEvent,
  payload?: { key?: string; format?: string; title?: string }
): Promise<{ canceled: boolean; path?: string }> {
  const format = payload?.format === 'md' || payload?.format === 'json' ? payload.format : null
  if (!format) throw new Error('导出格式仅支持 md / json')
  // 渲染层已知标题: 在线列表未命中(200 条上限外)时兜底, 避免文件名/标题回退成 id
  const titleHint = String(payload?.title ?? '').trim().slice(0, 200)
  const session = await loadExportSession(payload?.key, titleHint)
  const content = format === 'md' ? buildExportMarkdown(session) : buildExportJson(session)
  const win = BrowserWindow.fromWebContents(event.sender)
  const options = {
    defaultPath: join(app.getPath('documents'), `${sanitizeExportFileName(session.title)}.${format}`),
    filters:
      format === 'md'
        ? [{ name: 'Markdown', extensions: ['md'] }]
        : [{ name: 'JSON', extensions: ['json'] }]
  }
  const result = win ? await dialog.showSaveDialog(win, options) : await dialog.showSaveDialog(options)
  if (result.canceled || !result.filePath) return { canceled: true }
  try {
    writeFileSync(result.filePath, content, 'utf-8')
  } catch (err) {
    throw new Error(`写入失败: ${(err as Error).message}`)
  }
  return { canceled: false, path: result.filePath }
}

/** 归档/取消归档: 校验键与本地会话存在性; 临时会话拒绝归档; 返回最新归档集合 */
function handleArchiveSet(
  _event: IpcMainInvokeEvent,
  payload?: { key?: string; archived?: boolean }
): string[] {
  const key = normalizeArchiveKey(payload?.key)
  if (!key) throw new Error('归档键无效')
  const { mode, id } = parseSessionKey(key)
  if (mode !== 'local' && mode !== 'agent') throw new Error(`不支持的会话类型: ${mode}`)
  if (mode === 'local') {
    const meta = store.getSession(id)
    if (!meta) throw new Error('本地会话不存在')
    if (meta.ephemeral) throw new Error('临时会话不支持归档')
  }
  return archiveStore.set(key, payload?.archived === true)
}

/** 注册全部 localagent:* 通道与退出清理；在 app.whenReady 且 GATEWAY_DATA_DIR 就绪后调用 */
export function registerKernelIpc(): void {
  const dataDir = process.env.GATEWAY_DATA_DIR
  if (!dataDir) throw new Error('GATEWAY_DATA_DIR 未设置，无法初始化本地 agent 内核')

  // session store 内部自拼 localagent/sessions 子路径，这里只传数据目录
  store = createSessionStore(dataDir)
  // 归档集合与本地会话同在网关数据目录: <dataDir>/archived.json
  archiveStore = createArchiveStore(join(dataDir, 'archived.json'))
  skills = createSkillLoader(join(dataDir, 'localagent', 'skills'))

  // 启动清理: 上次退出/崩溃遗留的临时会话(仅本地)在此删除, 不进入历史
  const staleEphemeral = removeEphemeralSessions(store)
  if (staleEphemeral.length) console.log(`[kernel] 已清理 ${staleEphemeral.length} 个遗留临时会话`)

  // 权限网关：ask 同步调 notify，这里把请求转给该会话的活跃 sender；
  // 负载对齐 AgentEvent.permission_request 契约：sessionId 必带，能拿到活跃流时附 streamId
  permissions = createPermissions(
    (req) => {
      const list = pendingPermissionBySession.get(req.sessionId) ?? []
      list.push(req)
      pendingPermissionBySession.set(req.sessionId, list)
      const streamId = turns.streamIdOf(req.sessionId)
      turns.senderOf(req.sessionId)?.send('localagent:event', {
        type: 'permission_request',
        ...req,
        ...(streamId ? { streamId } : {})
      })
    },
    // 权限记忆绑当前数据目录：has 现读盘（respond(allow_always) 落盘后立即可见），add 原子落盘
    {
      has: (tool) => loadAlwaysAllowed(gatewayDataDir()).has(tool),
      add: (tool) => addAlwaysAllowed(gatewayDataDir(), tool)
    }
  )

  ipcMain.handle('localagent:sessions:list', () => store.listSessions())
  ipcMain.handle('localagent:sessions:create', handleSessionCreate)
  ipcMain.handle('localagent:sessions:delete', (_e, id: string) => store.deleteSession(id))
  ipcMain.handle('localagent:sessions:rename', (_e, payload?: { id?: string; title?: string }) =>
    store.renameSession(String(payload?.id ?? ''), String(payload?.title ?? ''))
  )
  ipcMain.handle('localagent:sessions:setModel', (_e, payload?: { id?: string; model?: string }) =>
    store.setModel(String(payload?.id ?? ''), String(payload?.model ?? ''))
  )
  ipcMain.handle('localagent:models:list', () => handleModelsList())
  ipcMain.handle('localagent:messages', (_e, id: string) => store.getMessages(id))
  ipcMain.handle('localagent:context:usage', handleContextUsage)
  ipcMain.handle('localagent:workspace:pick', handleWorkspacePick)
  ipcMain.handle('localagent:workspace:list', () => {
    requireSsoLogin()
    return { items: loadWorkspaces() }
  })
  ipcMain.handle('localagent:workspace:create', (_e, payload?: { name?: string }) => {
    requireSsoLogin()
    return createWorkspace(String(payload?.name ?? ''))
  })
  ipcMain.handle('localagent:file:pick', handleFilePick)
  ipcMain.handle('localagent:file:paste', handleFilePaste)
  ipcMain.handle('agent:attach:upload', handleAgentAttachUpload)
  ipcMain.handle('artifact:list', handleArtifactList)
  ipcMain.handle('artifact:read', handleArtifactRead)
  ipcMain.handle('artifact:reveal', handleArtifactReveal)
  ipcMain.handle('artifact:open', handleArtifactOpen)
  ipcMain.handle('localagent:attach:import', handleAttachImport)
  ipcMain.handle('localagent:chat', handleChat)
  // 消息级操作(仅本地会话): 重新生成 / 编辑重发, 复用 chat 的流式事件机制
  ipcMain.handle('localagent:regenerate', handleRegenerate)
  ipcMain.handle('localagent:editAndResend', handleEditAndResend)
  // 会话组织: 归档集合(本地维护, 两种模式通用)与导出(MD/JSON, 系统保存对话框)
  ipcMain.handle('sessions:archive:list', () => archiveStore.list())
  ipcMain.handle('sessions:archive:set', handleArchiveSet)
  ipcMain.handle('sessions:export', handleSessionExport)
  ipcMain.handle('localagent:stop', (_e, streamId: string) => {
    const entry = turns.streams.get(streamId)
    if (!entry) return
    entry.controller.abort()
    // 结算该会话全部未决权限（false），避免 loop 协程挂在 ask 上
    permissions.cancel(entry.sessionId, true)
    pendingPermissionBySession.delete(entry.sessionId)
  })
  ipcMain.handle('localagent:permissions-respond', (_e, payload: { requestId?: string; decision?: PermissionDecision }) => {
    const requestId = payload?.requestId
    const decision = payload?.decision
    if (!requestId || !decision) return
    permissions.respond(requestId, decision)
    // 从未决清单移除（respond 对未知 id 是 no-op，这里只维护自己的记录）
    for (const [sessionId, list] of pendingPermissionBySession) {
      const next = list.filter((r) => r.requestId !== requestId)
      if (next.length === list.length) continue
      if (next.length === 0) pendingPermissionBySession.delete(sessionId)
      else pendingPermissionBySession.set(sessionId, next)
    }
  })
  // 权限记忆为纯本地状态（同 mcp-prefs，不加 SSO 闸）：设置页查看/忘记/清空
  ipcMain.handle('localagent:permissions:list-remembered', () => ({ tools: listAlwaysAllowed(gatewayDataDir()) }))
  ipcMain.handle('localagent:permissions:forget', (_e, payload?: { tool?: string }) => {
    const tool = typeof payload?.tool === 'string' ? payload.tool.trim() : ''
    if (!tool) throw new Error('缺少工具名 tool')
    removeAlwaysAllowed(gatewayDataDir(), tool)
    return { ok: true }
  })
  ipcMain.handle('localagent:permissions:clear-remembered', () => {
    const count = listAlwaysAllowed(gatewayDataDir()).length
    clearAlwaysAllowed(gatewayDataDir())
    return { ok: true, count }
  })
  ipcMain.handle('localagent:skills:list', () => skills.listSkills())

  // 市场能力：订阅/安装/卸载/清单（全部走 SSO 身份校验）；人设列表读本地 agents 目录
  ipcMain.handle('localagent:market:listMy', () => handleMarketListMy())
  ipcMain.handle('localagent:market:subscribe', handleMarketSubscribe)
  ipcMain.handle('localagent:market:unsubscribe', handleMarketUnsubscribe)
  ipcMain.handle('localagent:market:install', handleMarketInstall)
  ipcMain.handle('localagent:market:uninstall', handleMarketUninstall)
  // 已安装清单与人设列表属市场侧本地视图，同样走 SSO 身份闸（与市场通道一致的登录前置）
  ipcMain.handle('localagent:market:listInstalled', () => {
    requireSsoLogin()
    return handleMarketListInstalled()
  })
  ipcMain.handle('localagent:personas:list', () => {
    requireSsoLogin()
    return listLocalPersonas()
  })
  ipcMain.handle('localagent:permission:mode', (_e, payload?: { mode?: string }) => {
    const mode = String(payload?.mode ?? '')
    if (mode === 'default' || mode === 'smart' || mode === 'auto') permissions.setMode(mode)
    return { mode: permissions.getMode() }
  })
  ipcMain.handle('localagent:mcp:status', () => {
    requireSsoLogin()
    return listMcpStatus()
  })
  // 连接器启用偏好为纯本地状态, 不加 SSO 闸（未登录也可先禁用/启用, 下一轮 chat 生效）
  ipcMain.handle('localagent:mcp:set-enabled', handleMcpSetEnabled)
  ipcMain.handle('localagent:mcp:env:get', handleMcpEnvGet)
  ipcMain.handle('localagent:mcp:env:set', handleMcpEnvSet)

  // MCP 连接句柄在退出时统一关闭；once 自包含在本模块，index.ts 无需关心
  app.once('will-quit', () => {
    void Promise.allSettled(mcpHandles.map((h) => h.close()))
  })

  // 退出删除临时会话(仅本地): 本次运行内可回看, 退出即删; 崩溃时由下次启动清理兜底
  app.on('before-quit', () => {
    try {
      removeEphemeralSessions(store)
    } catch (err) {
      console.warn(`[kernel] 临时会话清理失败: ${(err as Error).message}`)
    }
  })
}
