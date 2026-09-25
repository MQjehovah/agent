import { readFile } from 'node:fs/promises'
import type { Registry } from './registry'
import type { ToolDefinition, ToolResult } from './types'

/**
 * MCP 客户端接入：
 * - parseMcpConfig：解析 localagent/mcp.json 为服务端配置列表（纯函数）
 * - mcpToolName / wrapMcpTool：把 MCP 工具包装为内核 ToolDefinition（纯函数）
 * - connectMcpServers：拉起全部 stdio/SSE/Streamable HTTP/market-gateway 服务端并把工具注册进 Registry
 *
 * SDK（@modelcontextprotocol/sdk）为 ESM 包，统一在 connectMcpServers 内按需动态 import；
 * connector 与 resolveGateway 均可注入，保证纯函数与连接编排测试零 SDK 加载开销。
 */

/**
 * 单个 MCP 服务端配置：
 * - 本地进程用 command(+args)，远程用 url，二者互斥；
 * - market-gateway 不落 command/url，连接参数由注入的 resolveGateway 在运行时解析；
 * - transport 缺省时按 command→stdio、url→sse 推断（向后兼容）。
 */
export interface McpServerConfig {
  name: string
  command?: string
  args?: string[]
  /** stdio 附加环境变量（仅 command 条目有效；远端条目带 env 视为非法） */
  env?: Record<string, string>
  /** stdio 工作目录（仅 command 条目有效，必须非空字符串） */
  cwd?: string
  url?: string
  /** 显式传输方式；与配置形态必须一致（command 配 stdio，url 配 sse/http） */
  transport?: 'stdio' | 'sse' | 'http'
  /** 远程传输附加请求头，必须 string→string（仅 sse/http 有效） */
  headers?: Record<string, string>
  /** market-gateway = 直连平台 MCP 网关的能力端点（Streamable HTTP + Bearer） */
  kind?: 'plain' | 'market-gateway'
  /** 条目来源标记（如 capability: name@version），仅供展示 */
  source?: string
}

/** market-gateway 的运行时连接参数（由 ipc 注入的解析器提供） */
export interface McpGatewayTarget {
  url: string
  headers: Record<string, string>
}

/** 解析 market-gateway 能力的连接参数；前置条件不满足（如未登录）时抛中文错误 */
export type ResolveMcpGateway = (name: string) => Promise<McpGatewayTarget>

/** connectMcpServers 使用的客户端最小结构（解耦 SDK 具体类型） */
export interface MinimalMcpClient {
  connect(transport: unknown): Promise<void>
  callTool(args: { name: string; arguments?: Record<string, unknown> }): Promise<unknown>
  listTools(): Promise<{ tools?: unknown }>
  close(): Promise<void>
}

/**
 * 连接器：建立传输并完成 MCP 握手（失败时自行清理传输），返回已连接客户端。
 * 默认实现动态加载 SDK；测试注入假实现即可离线验证连接编排。
 */
export interface McpConnector {
  connect(cfg: McpServerConfig, target: McpGatewayTarget | null): Promise<MinimalMcpClient>
}

/** MCP listTools 返回的工具描述（仅取所需字段） */
export interface McpToolInfo {
  name: string
  description?: string
  inputSchema?: Record<string, unknown>
  /** MCP 规范工具注解(readOnlyHint/destructiveHint 等);非对象按「无注解」处理 */
  annotations?: Record<string, unknown>
}

/** 注入 wrapMcpTool 的调用委托，便于纯逻辑测试不触达真实 MCP */
export type McpCallTool = (args: Record<string, unknown>) => Promise<unknown>

/** 连接句柄：Task 9 在 app 退出时统一 close */
export interface McpConnection {
  server: string
  close(): Promise<void>
}

/** 取非空字符串，否则 null */
function nonEmpty(v: unknown): string | null {
  return typeof v === 'string' && v.length > 0 ? v : null
}

/** args 必须是字符串数组，否则 null（视为该条配置非法） */
function normalizeArgs(v: unknown): string[] | null {
  if (!Array.isArray(v) || !v.every(x => typeof x === 'string')) return null
  return [...v]
}

/** headers/env 必须是 string→string 的普通对象，否则 null（视为该条配置非法） */
function normalizeStringMap(v: unknown): Record<string, string> | null {
  if (typeof v !== 'object' || v === null || Array.isArray(v)) return null
  const out: Record<string, string> = {}
  for (const [key, value] of Object.entries(v as Record<string, unknown>)) {
    if (typeof value !== 'string') return null
    out[key] = value
  }
  return out
}

/**
 * 单条服务端配置校验与规范化；非法返回 null（调用方跳过该条）：
 * - 缺 name / name 非非空字符串
 * - command 与 url 同时存在（二者互斥）；除 market-gateway 外同时缺失
 * - market-gateway 与 command/url/transport/headers/env/cwd 互斥（连接参数运行时由 resolveGateway 提供）
 * - args 存在但不是字符串数组；headers/env 存在但不是 string→string 对象；cwd 存在但非非空字符串
 * - transport 不在 {stdio,sse,http}，或与配置形态矛盾（command 配 sse/http、url 配 stdio）
 * - kind 不是 plain / market-gateway
 */
function normalizeServer(item: unknown): McpServerConfig | null {
  if (typeof item !== 'object' || item === null || Array.isArray(item)) return null
  const raw = item as Record<string, unknown>
  const name = nonEmpty(raw.name)
  if (!name) return null
  const command = nonEmpty(raw.command)
  const url = nonEmpty(raw.url)
  if (command && url) return null // 二者互斥，同时存在非法

  let args: string[] | undefined
  if (raw.args !== undefined) {
    const normalized = normalizeArgs(raw.args)
    if (!normalized) return null
    args = normalized
  }

  let transport: McpServerConfig['transport']
  if (raw.transport !== undefined) {
    if (raw.transport !== 'stdio' && raw.transport !== 'sse' && raw.transport !== 'http') return null
    transport = raw.transport
  }

  let headers: Record<string, string> | undefined
  if (raw.headers !== undefined) {
    const normalized = normalizeStringMap(raw.headers)
    if (!normalized) return null
    headers = normalized
  }

  let env: Record<string, string> | undefined
  if (raw.env !== undefined) {
    const normalized = normalizeStringMap(raw.env)
    if (!normalized) return null
    env = normalized
  }

  let cwd: string | undefined
  if (raw.cwd !== undefined) {
    if (typeof raw.cwd !== 'string' || raw.cwd.trim().length === 0) return null
    cwd = raw.cwd
  }

  let kind: McpServerConfig['kind']
  if (raw.kind !== undefined) {
    if (raw.kind !== 'plain' && raw.kind !== 'market-gateway') return null
    kind = raw.kind
  }

  const source = nonEmpty(raw.source)

  // market-gateway：不落连接参数，运行时由 resolveGateway(name) 解析 url+headers
  if (kind === 'market-gateway') {
    if (command || url || transport !== undefined || headers !== undefined || env !== undefined || cwd !== undefined) {
      return null
    }
    const cfg: McpServerConfig = { name, kind: 'market-gateway' }
    if (source) cfg.source = source
    return cfg
  }

  if (!command && !url) return null // 二者必须择一（gateway 除外）

  if (command) {
    if (transport !== undefined && transport !== 'stdio') return null // 与显式 transport 矛盾
    if (headers !== undefined) return null // stdio 无 headers 语义
    const cfg: McpServerConfig = { name, command }
    if (args !== undefined) cfg.args = args
    if (env !== undefined) cfg.env = env
    if (cwd !== undefined) cfg.cwd = cwd
    if (transport) cfg.transport = transport
    if (source) cfg.source = source
    return cfg
  }

  if (transport === 'stdio') return null // url 条目与 stdio 矛盾
  if (env !== undefined || cwd !== undefined) return null // env/cwd 仅 stdio 语义
  const cfg: McpServerConfig = { name, url: url! }
  if (transport !== undefined) cfg.transport = transport
  if (headers !== undefined) cfg.headers = headers
  if (source) cfg.source = source
  return cfg
}

/**
 * 解析 mcp.json 原文为服务端配置列表。
 * 语义：整份配置层面的错误抛中文异常（JSON 损坏 / 顶层非对象 / servers 非数组）；
 * 单条配置非法（缺 name、command 与 url 互斥违反、args 类型错误等）跳过；
 * 服务端重名保留首个、跳过后续。
 */
export function parseMcpConfig(raw: string): McpServerConfig[] {
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch (e) {
    throw new Error(`MCP 配置解析失败: ${e instanceof Error ? e.message : String(e)}`)
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error('MCP 配置无效: 顶层必须是对象')
  }
  const servers = (parsed as { servers?: unknown }).servers
  if (!Array.isArray(servers)) throw new Error('MCP 配置无效: servers 必须是数组')
  const out: McpServerConfig[] = []
  const seen = new Set<string>()
  for (const item of servers) {
    const cfg = normalizeServer(item)
    if (!cfg || seen.has(cfg.name)) continue
    seen.add(cfg.name)
    out.push(cfg)
  }
  return out
}

/** MCP 工具在注册表中的命名空间化名称 */
export function mcpToolName(server: string, tool: string): string {
  return `mcp__${server}__${tool}`
}

/** 把 MCP callTool 结果序列化为 ToolResult：content 里的 text 块拼接；isError 时 ok:false；无 text 块回退 JSON 序列化 */
export function serializeMcpResult(result: unknown): ToolResult {
  const obj = (typeof result === 'object' && result !== null ? result : {}) as { content?: unknown; isError?: unknown }
  const texts: string[] = []
  if (Array.isArray(obj.content)) {
    for (const block of obj.content) {
      const b = block as { type?: unknown; text?: unknown }
      if (b.type === 'text' && typeof b.text === 'string') texts.push(b.text)
    }
  }
  const output = texts.length > 0 ? texts.join('\n') : JSON.stringify(result) ?? ''
  return { ok: obj.isError !== true, output }
}

/** annotations 必须是普通对象；数组/null/原始值视为无注解（不可信输入） */
function normalizeAnnotations(v: unknown): Record<string, unknown> | undefined {
  if (typeof v !== 'object' || v === null || Array.isArray(v)) return undefined
  return { ...(v as Record<string, unknown>) }
}

/**
 * MCP 工具注解 → 本地权限 kind：
 * 仅 annotations.readOnlyHint === true 判为 'read'（smart/default 均不弹确认）；
 * 其余（缺注解 / readOnlyHint=false / destructiveHint=true）一律 'write'。
 * 不做条目级 read 降级：能力包 risk_default 均为默认值，直接信任会过度放权，注解是唯一权威来源。
 */
function mcpToolKind(tool: McpToolInfo): 'read' | 'write' {
  return tool.annotations?.readOnlyHint === true ? 'read' : 'write'
}

/**
 * 把 MCP 工具描述包装为内核 ToolDefinition（纯逻辑，不发起真实调用）：
 * kind 由注解映射（readOnlyHint=true → 'read'，其余 → 'write'），
 * parameters 取工具 inputSchema（缺省给空对象 schema），execute 委托注入的调用函数并把结果序列化为文本。
 */
export function wrapMcpTool(opts: { server: string; tool: McpToolInfo; callTool: McpCallTool }): ToolDefinition {
  const { server, tool, callTool } = opts
  return {
    name: mcpToolName(server, tool.name),
    description: `[mcp:${server}] ${tool.description ?? tool.name}`,
    kind: mcpToolKind(tool),
    parameters: tool.inputSchema ?? { type: 'object', properties: {} },
    // callTool 可能 reject（服务端崩溃 / McpError / 超时），与内置工具一致折叠为 ok:false，不让异常逸出
    execute: async (args) => {
      try {
        return serializeMcpResult(await callTool(args))
      } catch (e) {
        return { ok: false, output: e instanceof Error ? e.message : String(e) }
      }
    }
  }
}

/** SDK 默认连接器：动态 import ESM-only SDK，按网关解析结果/显式 transport 构造传输 */
async function createSdkConnector(): Promise<McpConnector> {
  const [
    { Client },
    { StdioClientTransport, getDefaultEnvironment },
    { SSEClientTransport },
    { StreamableHTTPClientTransport }
  ] = await Promise.all([
    import('@modelcontextprotocol/sdk/client/index.js'),
    import('@modelcontextprotocol/sdk/client/stdio.js'),
    import('@modelcontextprotocol/sdk/client/sse.js'),
    import('@modelcontextprotocol/sdk/client/streamableHttp.js')
  ])
  return {
    connect: async (cfg, target) => {
      let transport: { close(): Promise<void> }
      if (target) {
        // market-gateway：平台能力端点走 Streamable HTTP（SDK 自动处理 initialize/session/通知）
        transport = new StreamableHTTPClientTransport(new URL(target.url), {
          requestInit: { headers: target.headers }
        })
      } else if (cfg.transport === 'http') {
        transport = new StreamableHTTPClientTransport(new URL(cfg.url!), {
          requestInit: { headers: cfg.headers ?? {} }
        })
      } else if (cfg.command) {
        // 本地 stdio：显式继承 SDK 默认环境（PATH 等）并叠加配置 env；cwd 缺省不传（由 SDK 用进程工作目录）
        const stdio = {
          command: cfg.command,
          args: cfg.args ?? [],
          env: { ...getDefaultEnvironment(), ...(cfg.env ?? {}) }
        } as { command: string; args: string[]; env: Record<string, string>; cwd?: string }
        if (cfg.cwd) stdio.cwd = cfg.cwd
        transport = new StdioClientTransport(stdio)
      } else {
        transport = new SSEClientTransport(
          new URL(cfg.url!),
          cfg.headers ? { requestInit: { headers: cfg.headers } } : undefined
        )
      }
      const client: MinimalMcpClient = new Client({ name: 'dashboard-agent', version: '0.1.0' })
      try {
        await client.connect(transport)
      } catch (e) {
        // 握手失败时回收传输（stdio 场景避免残留子进程、HTTP 场景关闭会话）
        await transport.close().catch(() => {})
        throw e
      }
      return client
    }
  }
}

/** 远程字段占位符：仅 url 与 headers 值扫描，形如 ${MARKET_URL} */
const MCP_VAR_RE = /\$\{([A-Z0-9_]+)\}/g

/** 单个字符串的占位符替换结果：替换后文本，或首个缺失变量名与提示 */
type VarSubstitution = { value: string } | { missing: string; hint: string }

/**
 * 替换字符串中的 ${VAR}：
 * - 已注册变量 → 运行时值；
 * - 未注册（undefined）或解析器抛错 → 返回缺失变量，调用方跳过整个 server
 *   （绝不发出含字面 ${X} 的请求）；
 * - MARKET_TOKEN 缺失时附「请先完成企业 SSO 登录」提示（未登录的企业身份场景）。
 */
function substituteVarString(value: string, resolveVars: (name: string) => string | undefined): VarSubstitution {
  let missing = ''
  let hint = ''
  const out = value.replace(MCP_VAR_RE, (literal, name: string) => {
    if (missing) return literal
    try {
      const resolved = resolveVars(name)
      if (resolved !== undefined) return resolved
      missing = name
      hint = name === 'MARKET_TOKEN' ? '请先完成企业 SSO 登录' : ''
    } catch (e) {
      missing = name
      hint = e instanceof Error ? e.message : String(e)
    }
    return literal
  })
  return missing ? { missing, hint } : { value: out }
}

/**
 * 对单条 server 做占位符替换：仅 url 与 headers 值（command/args/env/cwd 绝不替换）。
 * 无 url/headers（如旧 kind:market-gateway 条目）不触碰；存在缺失变量时返回 missing。
 */
function substituteServerVars(
  cfg: McpServerConfig,
  resolveVars: (name: string) => string | undefined
): { cfg: McpServerConfig } | { missing: string; hint: string } {
  if (cfg.url === undefined && cfg.headers === undefined) return { cfg }
  let missing = ''
  let hint = ''
  const resolveValue = (value: string): string => {
    if (missing) return value
    const sub = substituteVarString(value, resolveVars)
    if ('missing' in sub) {
      missing = sub.missing
      hint = sub.hint
      return value
    }
    return sub.value
  }
  const next: McpServerConfig = { ...cfg }
  if (cfg.url !== undefined) next.url = resolveValue(cfg.url)
  if (cfg.headers !== undefined) {
    const headers: Record<string, string> = {}
    for (const [key, value] of Object.entries(cfg.headers)) headers[key] = resolveValue(value)
    next.headers = headers
  }
  return missing ? { missing, hint } : { cfg: next }
}

/**
 * 连接全部 MCP 服务端并注册工具（进程级由调用方保证只调一次，函数内不缓存）：
 * - 配置文件不存在 / 整份配置损坏：onStatus 警告后空跑（返回空数组）；
 * - isDisabled 判定为禁用的服务端：onStatus 提示后跳过，不连接不注册（不影响其它 server）；
 * - 远程条目 url/headers 里的 ${VAR}：注入 resolveVars 时按注册变量替换；
 *   遇未注册/不可用变量跳过该 server 并告警（绝不发出含字面 ${X} 的请求，不影响其它 server）；
 * - market-gateway 旧条目：调 resolveGateway(name) 解析 url+headers；未注入解析器则报告并跳过；
 * - 单个 server 连接失败（含网关解析失败）：onStatus 报告后继续下一个，不阻断其他 server；
 * - 工具名 = mcp__<server>__<tool>，重名已注册时跳过，避免 registry 抛错中断。
 * 返回连接句柄数组，供调用方在退出时统一 close。
 * connector/resolveGateway/resolveVars 可注入，离线单测不加载 SDK。
 */
export async function connectMcpServers(opts: {
  registry: Registry
  /** mcp.json 路径，不存在视为空配置 */
  configPath: string
  onStatus?: (msg: string) => void
  /** market-gateway 连接参数解析器（ipc 注入：marketUrl + SSO Bearer） */
  resolveGateway?: ResolveMcpGateway
  /** 配置占位符变量解析器（ipc 注入：仅注册变量 MARKET_URL/MARKET_TOKEN）；未注册返回 undefined */
  resolveVars?: (name: string) => string | undefined
  /** 连接器启用偏好（ipc 注入：读 mcp-prefs 禁用名单）；返回 true 的服务端跳过，缺省全部启用 */
  isDisabled?: (name: string) => boolean
  /** 传输/客户端工厂；缺省动态加载 SDK 实现 */
  connector?: McpConnector
}): Promise<McpConnection[]> {
  const { registry, configPath, onStatus, resolveGateway, resolveVars } = opts
  const status = (msg: string) => onStatus?.(msg)
  const errMessage = (e: unknown) => (e instanceof Error ? e.message : String(e))

  let raw: string
  try {
    raw = await readFile(configPath, 'utf8')
  } catch {
    status('MCP 配置不存在，跳过 MCP 服务端连接')
    return []
  }

  let configs: McpServerConfig[]
  try {
    configs = parseMcpConfig(raw)
  } catch (e) {
    status(`MCP 配置无效，跳过 MCP 服务端连接: ${errMessage(e)}`)
    return []
  }

  const connector = opts.connector ?? (await createSdkConnector())

  const connections: McpConnection[] = []
  for (const cfg of configs) {
    // 用户已禁用的连接器：不连接不注册（偏好缺省=启用，见 mcp-prefs.ts）
    if (opts.isDisabled?.(cfg.name)) {
      status(`MCP 服务端 ${cfg.name} 已禁用，跳过`)
      continue
    }
    // client 提到 try 外声明，连接后失败（listTools/注册）时才能回收
    let client: MinimalMcpClient | undefined
    try {
      // 远程占位符：仅替换 url/headers 值，未注册/不可用变量 → 跳过该 server（不影响其它 server）
      let effective = cfg
      if (resolveVars) {
        const sub = substituteServerVars(cfg, resolveVars)
        if ('missing' in sub) {
          const literal = '${' + sub.missing + '}'
          status(
            sub.hint
              ? `MCP 服务端 ${cfg.name} 引用了不可用变量 ${literal}（${sub.hint}），跳过`
              : `MCP 服务端 ${cfg.name} 配置引用了未注册变量 ${literal}，跳过`
          )
          continue
        }
        effective = sub.cfg
      }
      let target: McpGatewayTarget | null = null
      if (effective.kind === 'market-gateway') {
        if (!resolveGateway) {
          status(`MCP 服务端 ${effective.name} 为 market-gateway，缺少网关解析器，跳过`)
          continue
        }
        target = await resolveGateway(effective.name)
      }
      client = await connector.connect(effective, target)
      const listed = await client.listTools()
      const tools: McpToolInfo[] = Array.isArray(listed.tools)
        ? (listed.tools as { name?: unknown; description?: unknown; inputSchema?: unknown; annotations?: unknown }[])
          .filter(t => typeof t.name === 'string')
          .map(t => ({
            name: t.name as string,
            description: typeof t.description === 'string' ? t.description : undefined,
            inputSchema: typeof t.inputSchema === 'object' && t.inputSchema !== null
              ? { ...(t.inputSchema as Record<string, unknown>) }
              : undefined,
            // 注解透传（仅对象），供 wrapMcpTool 映射本地权限 kind
            annotations: normalizeAnnotations(t.annotations)
          }))
        : []
      let registered = 0
      for (const tool of tools) {
        const name = mcpToolName(cfg.name, tool.name)
        if (registry.get(name)) {
          status(`MCP 工具 ${name} 已注册，跳过`)
          continue
        }
        registry.register(wrapMcpTool({
          server: cfg.name,
          tool,
          callTool: (args) => client!.callTool({ name: tool.name, arguments: args })
        }))
        registered++
      }
      connections.push({ server: cfg.name, close: () => client!.close() })
      status(`MCP 服务端 ${cfg.name} 已连接，注册 ${registered} 个工具`)
    } catch (e) {
      if (client) await client.close().catch(() => {})
      status(`MCP 服务端 ${cfg.name} 连接失败: ${errMessage(e)}`)
    }
  }
  return connections
}
