import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  connectMcpServers,
  parseMcpConfig,
  mcpToolName,
  wrapMcpTool,
  type McpConnector,
  type McpGatewayTarget,
  type McpServerConfig
} from '../../electron/main/kernel/mcp'
import { createRegistry } from '../../electron/main/kernel/registry'
import { createPermissions } from '../../electron/main/kernel/permissions'
import type { ToolContext } from '../../electron/main/kernel/types'

const ctx: ToolContext = { workspace: 'w', sessionId: 's' }

test('mcp: parseMcpConfig 解析 name+command 组合（含 args）', () => {
  const raw = JSON.stringify({
    servers: [{ name: 'filesystem', command: 'npx', args: ['-y', '@modelcontextprotocol/server-filesystem', 'C:\\ws'] }]
  })
  assert.deepEqual(parseMcpConfig(raw), [{
    name: 'filesystem', command: 'npx', args: ['-y', '@modelcontextprotocol/server-filesystem', 'C:\\ws']
  }])
})

test('mcp: parseMcpConfig 解析 name+url 组合且 args 可选', () => {
  const raw = JSON.stringify({ servers: [{ name: 'remote', url: 'http://host/sse' }] })
  assert.deepEqual(parseMcpConfig(raw), [{ name: 'remote', url: 'http://host/sse' }])
  const raw2 = JSON.stringify({ servers: [{ name: 'proc', command: 'node' }] })
  assert.deepEqual(parseMcpConfig(raw2), [{ name: 'proc', command: 'node' }])
})

test('mcp: parseMcpConfig 跳过非法条目（缺 name / 无 command 无 url / 同时有 / args 非数组 / 非对象 / 空 name）', () => {
  const raw = JSON.stringify({
    servers: [
      { command: 'npx' },
      { name: 'no-target' },
      { name: 'both', command: 'a', url: 'http://h' },
      { name: 'bad-args', command: 'x', args: 'nope' },
      'not-an-object',
      { name: '', command: 'x' },
      { name: 'ok', command: 'run' }
    ]
  })
  assert.deepEqual(parseMcpConfig(raw), [{ name: 'ok', command: 'run' }])
})

test('mcp: parseMcpConfig 服务端重名时保留首个', () => {
  const raw = JSON.stringify({
    servers: [
      { name: 'a', command: 'x' },
      { name: 'a', url: 'http://h' },
      { name: 'b', command: 'y' }
    ]
  })
  assert.deepEqual(parseMcpConfig(raw), [{ name: 'a', command: 'x' }, { name: 'b', command: 'y' }])
})

test('mcp: parseMcpConfig 解析 market-gateway（无需 command/url，保留 source）', () => {
  const raw = JSON.stringify({
    servers: [{ name: 'marketplace', kind: 'market-gateway', source: 'capability: marketplace@1.0.0' }]
  })
  assert.deepEqual(parseMcpConfig(raw), [
    { name: 'marketplace', kind: 'market-gateway', source: 'capability: marketplace@1.0.0' }
  ])
})

test('mcp: parseMcpConfig market-gateway 与 command/url 互斥，未知 kind 跳过', () => {
  const raw = JSON.stringify({
    servers: [
      { name: 'gw1', kind: 'market-gateway', url: 'http://h' },
      { name: 'gw2', kind: 'market-gateway', command: 'node' },
      { name: 'bad-kind', command: 'x', kind: 'mystery' },
      { name: 'ok', kind: 'market-gateway' }
    ]
  })
  assert.deepEqual(parseMcpConfig(raw), [{ name: 'ok', kind: 'market-gateway' }])
})

test('mcp: parseMcpConfig 接受 transport（stdio/sse/http）并校验与形态一致', () => {
  const raw = JSON.stringify({
    servers: [
      { name: 'h', url: 'https://gw/stream', transport: 'http' },
      { name: 's', url: 'http://h/sse', transport: 'sse' },
      { name: 'p', command: 'node', transport: 'stdio' },
      { name: 'bad1', url: 'http://h/sse', transport: 'stdio' },
      { name: 'bad2', command: 'node', transport: 'http' },
      { name: 'bad3', url: 'http://h/sse', transport: 'websocket' }
    ]
  })
  assert.deepEqual(parseMcpConfig(raw), [
    { name: 'h', url: 'https://gw/stream', transport: 'http' },
    { name: 's', url: 'http://h/sse', transport: 'sse' },
    { name: 'p', command: 'node', transport: 'stdio' }
  ])
})

test('mcp: parseMcpConfig headers 必须 string→string（数组/非字符串值跳过，空对象合法）', () => {
  const raw = JSON.stringify({
    servers: [
      { name: 'ok', url: 'http://h/sse', headers: { Authorization: 'Bearer t' } },
      { name: 'bad-num', url: 'http://h', headers: { 'x-count': 1 } },
      { name: 'bad-arr', url: 'http://h', headers: ['a'] },
      { name: 'empty', url: 'http://h', headers: {} }
    ]
  })
  assert.deepEqual(parseMcpConfig(raw), [
    { name: 'ok', url: 'http://h/sse', headers: { Authorization: 'Bearer t' } },
    { name: 'empty', url: 'http://h', headers: {} }
  ])
})

test('mcp: parseMcpConfig stdio 解析 env(string→string,空值合法)与 cwd(非空字符串)', () => {
  const raw = JSON.stringify({
    servers: [
      { name: 'ok', command: 'node', env: { API_KEY: 'k', EMPTY: '' }, cwd: 'C:\\work\\cap' },
      { name: 'bad-env-num', command: 'node', env: { 'x-count': 1 } },
      { name: 'bad-env-arr', command: 'node', env: ['a'] },
      { name: 'bad-env-null', command: 'node', env: null },
      { name: 'bad-cwd-empty', command: 'node', cwd: '   ' },
      { name: 'bad-cwd-type', command: 'node', cwd: 3 }
    ]
  })
  assert.deepEqual(parseMcpConfig(raw), [
    { name: 'ok', command: 'node', env: { API_KEY: 'k', EMPTY: '' }, cwd: 'C:\\work\\cap' }
  ])
})

test('mcp: parseMcpConfig env/cwd 仅 stdio 语义（url 条目/gateway 带 env/cwd 跳过）', () => {
  const raw = JSON.stringify({
    servers: [
      { name: 'bad-url-env', url: 'http://h', env: { A: '1' } },
      { name: 'bad-url-cwd', url: 'http://h', cwd: 'C:\\w' },
      { name: 'bad-gw-env', kind: 'market-gateway', env: { A: '1' } },
      { name: 'bad-gw-cwd', kind: 'market-gateway', cwd: 'C:\\w' },
      { name: 'ok', command: 'node' }
    ]
  })
  assert.deepEqual(parseMcpConfig(raw), [{ name: 'ok', command: 'node' }])
})

test('mcp: parseMcpConfig JSON 损坏抛中文错误', () => {
  assert.throws(() => parseMcpConfig('{oops'), /MCP 配置/)
})

test('mcp: parseMcpConfig servers 非数组或缺失抛中文错误，顶层非对象抛中文错误', () => {
  assert.throws(() => parseMcpConfig(JSON.stringify({ servers: 'x' })), /servers 必须是数组/)
  assert.throws(() => parseMcpConfig('{}'), /servers 必须是数组/)
  assert.throws(() => parseMcpConfig('[]'), /顶层必须是对象/)
})

test('mcp: mcpToolName 拼接 mcp__<server>__<tool>', () => {
  assert.equal(mcpToolName('filesystem', 'read_file'), 'mcp__filesystem__read_file')
})

test('mcp: wrapMcpTool 无注解默认 write 类 ToolDefinition 且 execute 委托注入函数', async () => {
  const calls: Record<string, unknown>[] = []
  const tool = wrapMcpTool({
    server: 'fs',
    tool: {
      name: 'read',
      description: '读文件',
      inputSchema: { type: 'object', properties: { path: { type: 'string' } } }
    },
    callTool: async (args) => {
      calls.push(args)
      return { content: [{ type: 'text', text: 'hello' }, { type: 'image', data: 'x' }, { type: 'text', text: 'world' }] }
    }
  })
  assert.equal(tool.name, 'mcp__fs__read')
  assert.equal(tool.kind, 'write')
  assert.equal(tool.description, '[mcp:fs] 读文件')
  assert.deepEqual(tool.parameters, { type: 'object', properties: { path: { type: 'string' } } })
  const res = await tool.execute({ path: 'a.txt' }, ctx)
  assert.equal(res.ok, true)
  assert.equal(res.output, 'hello\nworld')
  assert.deepEqual(calls, [{ path: 'a.txt' }])
})

// —— 注解 → 本地权限 kind 映射（readOnlyHint 是唯一权威来源） ——

/** 复刻 loop.execToolCall 的弹窗判定：write 类且 needsAsk 才询问 */
function wouldAsk(tool: { name: string; kind?: 'read' | 'write' }, permissions: ReturnType<typeof createPermissions>): boolean {
  return tool.kind === 'write' && permissions.needsAsk(tool.name, tool.kind)
}

test('mcp: wrapMcpTool readOnlyHint=true → read，default/smart 均不弹确认（执行路径）', async () => {
  const tool = wrapMcpTool({
    server: 'time',
    tool: { name: 'time_now', annotations: { readOnlyHint: true } },
    callTool: async () => ({ content: [{ type: 'text', text: '2026-09-24' }] })
  })
  assert.equal(tool.kind, 'read')
  const permissions = createPermissions(() => {})
  for (const mode of ['default', 'smart'] as const) {
    permissions.setMode(mode)
    assert.equal(wouldAsk(tool, permissions), false, `${mode} 模式不应弹确认`)
  }
  const res = await tool.execute({}, ctx)
  assert.equal(res.ok, true)
  assert.equal(res.output, '2026-09-24')
})

test('mcp: wrapMcpTool readOnlyHint=false 或 destructiveHint=true → write（仍需确认）', () => {
  const permissions = createPermissions(() => {})
  permissions.setMode('smart')
  const cases: Array<Record<string, unknown>> = [
    { readOnlyHint: false },
    { destructiveHint: true },
    { readOnlyHint: false, destructiveHint: true },
    { readOnlyHint: 'yes' }
  ]
  for (const annotations of cases) {
    const tool = wrapMcpTool({ server: 's', tool: { name: 'mutate', annotations }, callTool: async () => ({}) })
    assert.equal(tool.kind, 'write', JSON.stringify(annotations))
    assert.equal(wouldAsk(tool, permissions), true, JSON.stringify(annotations))
  }
})

test('mcp: wrapMcpTool annotations 非对象忽略（按无注解=write 处理）', () => {
  for (const bad of ['nope', 1, true, null, []]) {
    const tool = wrapMcpTool({
      server: 's',
      tool: { name: 't', annotations: bad as unknown as Record<string, unknown> },
      callTool: async () => ({})
    })
    assert.equal(tool.kind, 'write', String(bad))
  }
})

test('mcp: wrapMcpTool 缺省 description/inputSchema 时给出回退值', async () => {
  const tool = wrapMcpTool({ server: 's', tool: { name: 't' }, callTool: async () => ({}) })
  assert.equal(tool.description, '[mcp:s] t')
  assert.deepEqual(tool.parameters, { type: 'object', properties: {} })
  const res = await tool.execute({}, ctx)
  assert.equal(res.ok, true)
  assert.deepEqual(JSON.parse(res.output), {})
})

test('mcp: wrapMcpTool isError 结果折叠为 ok:false', async () => {
  const tool = wrapMcpTool({
    server: 's',
    tool: { name: 't' },
    callTool: async () => ({ content: [{ type: 'text', text: 'boom' }], isError: true })
  })
  const res = await tool.execute({}, ctx)
  assert.equal(res.ok, false)
  assert.equal(res.output, 'boom')
})

test('mcp: wrapMcpTool callTool 抛异常时折叠为 ok:false 且 output 含错误文本', async () => {
  const tool = wrapMcpTool({
    server: 's',
    tool: { name: 't' },
    callTool: async () => { throw new Error('Connection closed') }
  })
  const res = await tool.execute({}, ctx)
  assert.equal(res.ok, false)
  assert.ok(res.output.includes('Connection closed'))
})

test('mcp: wrapMcpTool 无 text 块时回退 JSON 序列化', async () => {
  const tool = wrapMcpTool({
    server: 's',
    tool: { name: 't' },
    callTool: async () => ({ status: 'done' })
  })
  const res = await tool.execute({}, ctx)
  assert.equal(res.ok, true)
  assert.equal(res.output, '{"status":"done"}')
})

// ---------- connectMcpServers（注入假 connector/resolveGateway，离线） ----------

/** 假 MCP 客户端：记录 callTool 参数，close 后可断言 */
function fakeClient(
  tools: Array<{ name: string; description?: string; inputSchema?: Record<string, unknown>; annotations?: unknown }>,
  toolOutput = 'ok'
) {
  const calls: Array<{ name: string; arguments?: Record<string, unknown> }> = []
  let closed = false
  return {
    client: {
      connect: async () => {},
      listTools: async () => ({ tools }),
      callTool: async (args: { name: string; arguments?: Record<string, unknown> }) => {
        calls.push(args)
        return { content: [{ type: 'text', text: toolOutput }] }
      },
      close: async () => {
        closed = true
      }
    },
    calls,
    isClosed: () => closed
  }
}

/** 写临时 mcp.json，返回配置路径与清理用临时目录 */
function writeMcpConfig(servers: unknown[]): { dir: string; configPath: string } {
  const dir = mkdtempSync(join(tmpdir(), 'mcp-conn-'))
  const configPath = join(dir, 'mcp.json')
  writeFileSync(configPath, JSON.stringify({ servers }))
  return { dir, configPath }
}

test('mcp: connectMcpServers 用 resolveGateway 解析 gateway 的 url+headers 并注册工具', async (t) => {
  const { dir, configPath } = writeMcpConfig([
    { name: 'marketplace', kind: 'market-gateway', source: 'capability: marketplace@1.0.0' },
    { name: 'plain', command: 'node' }
  ])
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const registry = createRegistry()
  const seen: Array<{ cfg: McpServerConfig; target: McpGatewayTarget | null }> = []
  const fake = fakeClient([{ name: 'search' }, { name: 'list' }])
  const connector: McpConnector = {
    connect: async (cfg, target) => {
      seen.push({ cfg, target })
      return fake.client
    }
  }
  const statuses: string[] = []
  const handles = await connectMcpServers({
    registry,
    configPath,
    connector,
    resolveGateway: async (name) => ({
      url: `https://market.example/api/mcp-gateway/relay/${name}/stream`,
      headers: { Authorization: 'Bearer t' }
    }),
    onStatus: (m) => statuses.push(m)
  })
  assert.equal(seen.length, 2, 'gateway 与普通条目都应进入 connector')
  assert.equal(seen[0].cfg.kind, 'market-gateway')
  assert.deepEqual(seen[0].target, {
    url: 'https://market.example/api/mcp-gateway/relay/marketplace/stream',
    headers: { Authorization: 'Bearer t' }
  })
  assert.equal(seen[1].cfg.command, 'node')
  assert.equal(seen[1].target, null, '非 gateway 条目不解析连接参数')
  assert.ok(registry.get('mcp__marketplace__search'))
  assert.ok(registry.get('mcp__marketplace__list'))
  const tool = registry.get('mcp__marketplace__search')!
  const res = await tool.execute({ q: 'x' }, ctx)
  assert.equal(res.ok, true)
  assert.equal(res.output, 'ok')
  assert.deepEqual(fake.calls, [{ name: 'search', arguments: { q: 'x' } }])
  assert.equal(handles.length, 2)
  assert.ok(statuses.some((s) => s.includes('marketplace') && s.includes('2 个工具')), statuses.join('|'))
  await handles[0].close()
  assert.equal(fake.isClosed(), true)
})

test('mcp: connectMcpServers 缺 resolveGateway 时跳过 gateway 条目且不触达 connector', async (t) => {
  const { dir, configPath } = writeMcpConfig([{ name: 'marketplace', kind: 'market-gateway' }])
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const registry = createRegistry()
  let connectorCalled = false
  const statuses: string[] = []
  const handles = await connectMcpServers({
    registry,
    configPath,
    connector: {
      connect: async () => {
        connectorCalled = true
        return fakeClient([]).client
      }
    },
    onStatus: (m) => statuses.push(m)
  })
  assert.equal(handles.length, 0)
  assert.equal(connectorCalled, false)
  assert.ok(statuses.some((s) => s.includes('market-gateway') && s.includes('跳过')), statuses.join('|'))
})

test('mcp: connectMcpServers resolveGateway 抛错（未登录）时跳过该条且不影响其它 server', async (t) => {
  const { dir, configPath } = writeMcpConfig([
    { name: 'gateway-cap', kind: 'market-gateway' },
    { name: 'plain', command: 'node' }
  ])
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const registry = createRegistry()
  const statuses: string[] = []
  const handles = await connectMcpServers({
    registry,
    configPath,
    connector: { connect: async () => fakeClient([{ name: 'run' }]).client },
    resolveGateway: async () => {
      throw new Error('请先完成企业 SSO 登录')
    },
    onStatus: (m) => statuses.push(m)
  })
  assert.deepEqual(handles.map((h) => h.server), ['plain'])
  assert.ok(registry.get('mcp__plain__run'))
  assert.ok(
    statuses.some((s) => s.includes('gateway-cap') && s.includes('请先完成企业 SSO 登录')),
    statuses.join('|')
  )
})

test('mcp: connectMcpServers 注入 resolveVars 时替换 url/headers 占位符后连接（不改写磁盘配置）', async (t) => {
  const { dir, configPath } = writeMcpConfig([
    {
      name: 'time',
      transport: 'http',
      url: '${MARKET_URL}/api/mcp-gateway/relay/time/stream',
      headers: { Authorization: 'Bearer ${MARKET_TOKEN}', 'X-Trace': 'prod-${MARKET_TOKEN}-end' },
      source: 'capability: time@1.0.1'
    }
  ])
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const registry = createRegistry()
  const seen: McpServerConfig[] = []
  const statuses: string[] = []
  const handles = await connectMcpServers({
    registry,
    configPath,
    connector: {
      connect: async (cfg) => {
        seen.push(cfg)
        return fakeClient([{ name: 'time_now' }]).client
      }
    },
    resolveVars: (name) =>
      name === 'MARKET_URL' ? 'https://market.example' : name === 'MARKET_TOKEN' ? 'tok-123' : undefined,
    onStatus: (m) => statuses.push(m)
  })
  assert.equal(seen.length, 1)
  assert.equal(seen[0].url, 'https://market.example/api/mcp-gateway/relay/time/stream')
  assert.deepEqual(seen[0].headers, {
    Authorization: 'Bearer tok-123',
    'X-Trace': 'prod-tok-123-end'
  })
  assert.ok(registry.get('mcp__time__time_now'))
  assert.equal(handles.length, 1)
  assert.ok(statuses.some((s) => s.includes('time') && s.includes('1 个工具')), statuses.join('|'))
  const onDisk = readFileSync(configPath, 'utf8')
  assert.ok(onDisk.includes('${MARKET_URL}') && onDisk.includes('${MARKET_TOKEN}'), '磁盘配置保留占位符')
})

test('mcp: connectMcpServers 未注册变量跳过该 server 且不触达 connector（不影响其它 server）', async (t) => {
  const { dir, configPath } = writeMcpConfig([
    { name: 'bad-var', transport: 'http', url: 'https://${PRIVATE_HOST}/stream' },
    { name: 'plain', command: 'node' }
  ])
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const registry = createRegistry()
  const seen: McpServerConfig[] = []
  const statuses: string[] = []
  const handles = await connectMcpServers({
    registry,
    configPath,
    connector: {
      connect: async (cfg) => {
        seen.push(cfg)
        return fakeClient([{ name: 'run' }]).client
      }
    },
    resolveVars: () => undefined,
    onStatus: (m) => statuses.push(m)
  })
  assert.deepEqual(seen.map((c) => c.name), ['plain'], '含未注册变量的 server 不应触达 connector')
  assert.equal(handles.length, 1)
  assert.ok(
    statuses.some((s) => s.includes('bad-var') && s.includes('${PRIVATE_HOST}') && s.includes('未注册')),
    statuses.join('|')
  )
})

test('mcp: connectMcpServers MARKET_TOKEN 缺失（未登录）时跳过并提示 SSO 登录', async (t) => {
  const { dir, configPath } = writeMcpConfig([
    {
      name: 'time',
      transport: 'http',
      url: '${MARKET_URL}/api/mcp-gateway/relay/time/stream',
      headers: { Authorization: 'Bearer ${MARKET_TOKEN}' }
    }
  ])
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const registry = createRegistry()
  let connectorCalled = false
  const statuses: string[] = []
  const handles = await connectMcpServers({
    registry,
    configPath,
    connector: {
      connect: async () => {
        connectorCalled = true
        return fakeClient([]).client
      }
    },
    resolveVars: (name) => (name === 'MARKET_URL' ? 'https://market.example' : undefined),
    onStatus: (m) => statuses.push(m)
  })
  assert.equal(connectorCalled, false)
  assert.equal(handles.length, 0)
  assert.ok(statuses.some((s) => s.includes('time') && s.includes('请先完成企业 SSO 登录')), statuses.join('|'))
})

test('mcp: connectMcpServers 占位符只作用于 url/headers（command/args/env 里的 ${X} 原样保留）', async (t) => {
  const { dir, configPath } = writeMcpConfig([
    {
      name: 'local-cap',
      command: 'python',
      args: ['${MARKET_URL}/server.py'],
      env: { TOKEN: '${MARKET_TOKEN}', MODE: 'local' },
      cwd: 'C:\\data\\mcp-servers\\local-cap'
    },
    { name: 'remote-cap', transport: 'http', url: '${MARKET_URL}/stream' }
  ])
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const registry = createRegistry()
  const seen: McpServerConfig[] = []
  const handles = await connectMcpServers({
    registry,
    configPath,
    connector: {
      connect: async (cfg) => {
        seen.push(cfg)
        return fakeClient([{ name: 'now' }]).client
      }
    },
    resolveVars: () => 'replaced'
  })
  assert.equal(seen.length, 2)
  assert.deepEqual(seen[0].args, ['${MARKET_URL}/server.py'], 'args 不替换')
  assert.deepEqual(seen[0].env, { TOKEN: '${MARKET_TOKEN}', MODE: 'local' }, 'env 不替换')
  assert.equal(seen[1].url, 'replaced/stream', 'url 正常替换')
  assert.equal(handles.length, 2)
})

test('mcp: connectMcpServers 无占位符时行为不变（即便注入 resolveVars）', async (t) => {
  const { dir, configPath } = writeMcpConfig([
    { name: 'plain', transport: 'http', url: 'https://gw/stream', headers: { Authorization: 'Bearer literal' } }
  ])
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const registry = createRegistry()
  const seen: McpServerConfig[] = []
  const handles = await connectMcpServers({
    registry,
    configPath,
    connector: {
      connect: async (cfg) => {
        seen.push(cfg)
        return fakeClient([{ name: 'run' }]).client
      }
    },
    resolveVars: () => undefined
  })
  assert.equal(seen[0].url, 'https://gw/stream')
  assert.deepEqual(seen[0].headers, { Authorization: 'Bearer literal' })
  assert.ok(registry.get('mcp__plain__run'))
  assert.equal(handles.length, 1)
})

test('mcp: connectMcpServers isDisabled 的服务端跳过（status 文案+不连接+不注册，不影响其它 server）', async (t) => {
  const { dir, configPath } = writeMcpConfig([
    { name: 'off-cap', command: 'node' },
    { name: 'on-cap', command: 'node' }
  ])
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const registry = createRegistry()
  const seen: McpServerConfig[] = []
  const statuses: string[] = []
  const handles = await connectMcpServers({
    registry,
    configPath,
    isDisabled: (name) => name === 'off-cap',
    connector: {
      connect: async (cfg) => {
        seen.push(cfg)
        return fakeClient([{ name: 'run' }]).client
      }
    },
    onStatus: (m) => statuses.push(m)
  })
  assert.deepEqual(seen.map((c) => c.name), ['on-cap'], '禁用条目不应触达 connector')
  assert.equal(handles.length, 1)
  assert.equal(registry.get('mcp__off-cap__run'), undefined, '禁用条目不应注册任何工具')
  assert.ok(registry.get('mcp__on-cap__run'))
  assert.ok(
    statuses.some((s) => s.includes('off-cap') && s.includes('已禁用') && s.includes('跳过')),
    statuses.join('|')
  )
})

test('mcp: connectMcpServers 不注入 isDisabled 时全部照常连接（缺省=启用）', async (t) => {
  const { dir, configPath } = writeMcpConfig([{ name: 'cap', command: 'node' }])
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const registry = createRegistry()
  const handles = await connectMcpServers({
    registry,
    configPath,
    connector: { connect: async () => fakeClient([{ name: 'run' }]).client }
  })
  assert.equal(handles.length, 1)
  assert.ok(registry.get('mcp__cap__run'))
})

test('mcp: connectMcpServers 把 stdio 的 env/cwd 原样传入 connector(本地安装条目)', async (t) => {
  const { dir, configPath } = writeMcpConfig([
    {
      name: 'local-cap',
      command: 'python',
      args: ['C:\\data\\mcp-servers\\local-cap\\server.py'],
      env: { TOKEN: '${TOKEN}', MODE: 'local' },
      cwd: 'C:\\data\\mcp-servers\\local-cap',
      source: 'capability: local-cap@1.0.0'
    }
  ])
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const registry = createRegistry()
  const seen: McpServerConfig[] = []
  const handles = await connectMcpServers({
    registry,
    configPath,
    connector: {
      connect: async (cfg) => {
        seen.push(cfg)
        return fakeClient([{ name: 'now' }]).client
      }
    }
  })
  assert.equal(seen.length, 1)
  assert.deepEqual(seen[0].env, { TOKEN: '${TOKEN}', MODE: 'local' })
  assert.equal(seen[0].cwd, 'C:\\data\\mcp-servers\\local-cap')
  assert.ok(registry.get('mcp__local-cap__now'))
  assert.equal(handles.length, 1)
})

test('mcp: connectMcpServers 透传 listTools 注解（readOnlyHint=true 注册为 read，非对象忽略）', async (t) => {
  const { dir, configPath } = writeMcpConfig([{ name: 'cap', command: 'node' }])
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const registry = createRegistry()
  const handles = await connectMcpServers({
    registry,
    configPath,
    connector: {
      connect: async () => fakeClient([
        { name: 'time_now', description: '当前时间', annotations: { readOnlyHint: true, idempotentHint: true } },
        { name: 'list_devices', annotations: { readOnlyHint: false } },
        { name: 'delete_device', annotations: { destructiveHint: true } },
        { name: 'mutate', annotations: 'not-an-object' },
        { name: 'plain' }
      ]).client
    }
  })
  assert.equal(handles.length, 1)
  assert.equal(registry.get('mcp__cap__time_now')!.kind, 'read')
  assert.equal(registry.get('mcp__cap__list_devices')!.kind, 'write')
  assert.equal(registry.get('mcp__cap__delete_device')!.kind, 'write')
  assert.equal(registry.get('mcp__cap__mutate')!.kind, 'write')
  assert.equal(registry.get('mcp__cap__plain')!.kind, 'write')
})
