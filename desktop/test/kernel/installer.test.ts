import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { isAbsolute, join } from 'node:path'
import { tmpdir } from 'node:os'
import { zipSync, strToU8 } from 'fflate'
import { installCapability, removeDirWithRetry, shouldRefuseRemoteInstall, uninstallCapability, unzipSafe } from '../../electron/main/kernel/installer'
import { createSkillLoader } from '../../electron/main/kernel/skills'
import type { MarketCapabilityType } from '../../electron/main/kernel/market'

/** 用 key->text 构造 zip 字节；strToU8 保证 utf-8 */
function makeZip(files: Record<string, string>): Buffer {
  const entries: Record<string, Uint8Array> = {}
  for (const [name, text] of Object.entries(files)) entries[name] = strToU8(text)
  return Buffer.from(zipSync(entries))
}

function skillsDir(dataDir: string): string {
  return join(dataDir, 'localagent', 'skills')
}

function agentsDir(dataDir: string): string {
  return join(dataDir, 'localagent', 'agents')
}

function mcpJsonPath(dataDir: string): string {
  return join(dataDir, 'localagent', 'mcp.json')
}

function readMcpServers(dataDir: string): unknown[] {
  return JSON.parse(readFileSync(mcpJsonPath(dataDir), 'utf8')).servers as unknown[]
}

/** 预置 mcp.json(自动建 localagent 目录),content 为原始字符串 */
function writeMcpRaw(dataDir: string, content: string): void {
  mkdirSync(join(dataDir, 'localagent'), { recursive: true })
  writeFileSync(mcpJsonPath(dataDir), content)
}

function readSkill(dataDir: string, name: string): string {
  return readFileSync(join(skillsDir(dataDir), name, 'SKILL.md'), 'utf8')
}

function install(
  dataDir: string,
  type: MarketCapabilityType,
  name: string,
  artifact: Buffer,
  extra: { version?: string; description?: string; mode?: 'platform' | 'local'; confirmed?: boolean } = {}
) {
  return installCapability({
    dataDir,
    type,
    name,
    version: extra.version ?? '1.0.0',
    artifact,
    description: extra.description,
    mode: extra.mode,
    // 本地 stdio MCP 需确认: 测试默认视为已确认(同意流程单独覆盖)
    confirmed: extra.confirmed ?? true
  })
}

// ---------- skill ----------

test('installer skill：无 front-matter 时补写 name/description(description 用入参兜底)', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-skill-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const artifact = makeZip({
    'skill.json': '{"name":"doc-check","description":"检查文档规范"}',
    'SKILL.md': '# 检查文档\n\n正文内容\n'
  })
  const res = await install(dataDir, 'skill', 'doc-check', artifact, {
    description: '入参兜底描述'
  })
  assert.equal(res.ok, true)
  assert.ok(res.output.includes('doc-check'), res.output)
  const text = readSkill(dataDir, 'doc-check')
  assert.ok(text.startsWith('---\n'), `应补写 front-matter: ${text}`)
  assert.ok(text.includes('name: doc-check'), text)
  assert.ok(text.includes('description: 入参兜底描述'), text)
  assert.ok(text.includes('# 检查文档'), text)
  assert.ok(existsSync(join(skillsDir(dataDir), 'doc-check', 'skill.json')), '包内其余文件一并落盘')
})

test('installer skill：已带 name+description 的 front-matter 原样保留', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-skill2-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const original = '---\nname: doc-check\ndescription: 包自带描述\n---\n# 正文\n'
  const res = await install(dataDir, 'skill', 'doc-check', makeZip({ 'SKILL.md': original }), {
    description: '兜底'
  })
  assert.equal(res.ok, true)
  assert.equal(readSkill(dataDir, 'doc-check'), original, '已有 front-matter 不应改动')
})

test('installer skill：front-matter 缺 description 时补入参描述、缺 name 时补能力名', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-skill3-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const res = await install(
    dataDir,
    'skill',
    'doc-check',
    makeZip({ 'SKILL.md': '---\nname: doc-check\n---\n# 正文\n' }),
    { description: '兜底描述' }
  )
  assert.equal(res.ok, true)
  const text = readSkill(dataDir, 'doc-check')
  assert.ok(text.includes('description: 兜底描述'), text)
  assert.ok(text.includes('# 正文'), text)
})

test('installer skill：description 值为空视为缺失，就地补入参描述且 listSkills 可见', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-skill5-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const res = await install(
    dataDir,
    'skill',
    'doc-check',
    makeZip({ 'SKILL.md': '---\nname: doc-check\ndescription:\n---\n# 正文\n' }),
    { description: '入参兜底描述' }
  )
  assert.equal(res.ok, true)
  const text = readSkill(dataDir, 'doc-check')
  assert.ok(text.includes('description: 入参兜底描述'), text)
  assert.ok(text.includes('# 正文'), text)
  const loader = createSkillLoader(skillsDir(dataDir))
  assert.deepEqual(loader.listSkills(), [{ name: 'doc-check', description: '入参兜底描述' }])
})

test('installer skill：包缺少 SKILL.md 时返回 ok:false', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-skill4-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const res = await install(dataDir, 'skill', 'doc-check', makeZip({ 'skill.json': '{}' }))
  assert.equal(res.ok, false)
  assert.ok(res.output.includes('SKILL.md'), res.output)
})

// ---------- zip-slip ----------

test('installer unzipSafe：拒绝 ../、绝对路径、盘符条目(zip-slip)', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-slip-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const out = join(dataDir, 'out')
  mkdirSync(out)
  const attacks: Array<Record<string, string>> = [
    { '../evil.txt': 'x' },
    { '/abs/evil.txt': 'x' },
    { 'C:/evil.txt': 'x' },
    { 'a/../../evil.txt': 'x' }
  ]
  for (const files of attacks) {
    await assert.rejects(unzipSafe(makeZip(files), out), (err: unknown) => {
      const message = err instanceof Error ? err.message : String(err)
      assert.ok(message.includes('非法路径'), `应提示非法路径: ${message}`)
      return true
    })
  }
  assert.ok(!existsSync(join(dataDir, 'evil.txt')), '不得写入目标目录之外')
  assert.ok(!existsSync(join(out, 'evil.txt')), 'zip-slip 样本应整体拒绝')
})

test('installer unzipSafe：正常嵌套条目落盘并返回文件名列表', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-safe-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const out = join(dataDir, 'out')
  const written = await unzipSafe(makeZip({ 'SKILL.md': '# s', 'scripts/run.py': 'print(1)' }), out)
  assert.deepEqual(written, ['SKILL.md', 'scripts/run.py'])
  assert.equal(readFileSync(join(out, 'scripts', 'run.py'), 'utf8'), 'print(1)')
})

test('installer 恶意能力包(含 zip-slip)整体折叠为 ok:false 且不外写', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mal-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const res = await install(dataDir, 'skill', 'doc-check', makeZip({ '../evil.txt': 'x', 'SKILL.md': '# s' }))
  assert.equal(res.ok, false)
  assert.ok(res.output.includes('非法路径'), res.output)
  assert.ok(!existsSync(join(dataDir, 'evil.txt')))
})

// ---------- mcp ----------

test('installer mcp：connection.json 可本地 spawn 时写 command/args 条目，覆盖同名并保留其它', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  writeMcpRaw(
    dataDir,
    JSON.stringify({
      servers: [
        { name: 'keep-me', command: 'npx', args: ['-y', 'some-server'] },
        { name: 'demo-mcp', command: 'old', args: ['old-arg'] }
      ]
    })
  )
  const artifact = makeZip({
    'mcp.json': '{"name":"demo-mcp"}',
    'connection.json': JSON.stringify({ transport: 'stdio', command: 'python', args: ['-m', 'demo_mcp'] }),
    'tools.json': '{"tools":[]}',
    'security.json': '{"sandbox":false}'
  })
  const res = await install(dataDir, 'mcp', 'demo-mcp', artifact, { version: '2.0.0' })
  assert.equal(res.ok, true)
  const servers = readMcpServers(dataDir) as Array<Record<string, unknown>>
  assert.equal(servers.length, 2, '同名条目应覆盖而非追加')
  const keep = servers.find((s) => s.name === 'keep-me')
  assert.deepEqual(keep, { name: 'keep-me', command: 'npx', args: ['-y', 'some-server'] })
  const installed = servers.find((s) => s.name === 'demo-mcp')
  assert.equal(installed?.command, 'python')
  assert.deepEqual(installed?.args, ['-m', 'demo_mcp'])
  assert.equal(installed?.source, 'capability: demo-mcp@2.0.0')
})

test('installer mcp：gateway 连接落 kind:market-gateway 条目（运行时由平台网关解析 url/headers）', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp2-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  // transport=gateway 直连平台 MCP 网关能力端点，连接参数由本地 agent 运行时用 marketUrl+SSO token 解析
  const gateway = makeZip({
    'connection.json': JSON.stringify({ transport: 'gateway', server: 'gw-prod' })
  })
  const res = await install(dataDir, 'mcp', 'gw-cap', gateway, { version: '0.3.0' })
  assert.equal(res.ok, true)
  const item = (readMcpServers(dataDir) as Array<Record<string, unknown>>).find((s) => s.name === 'gw-cap')
  assert.equal(item?.kind, 'market-gateway')
  assert.equal(item?.source, 'capability: gw-cap@0.3.0')
  assert.equal(item?.command, undefined, 'gateway 条目不落 command')
  assert.equal(item?.url, undefined, 'gateway 条目不落 url')
})

test('installer mcp：http/streamable_http + url + 无 headers 落 transport:http 条目（本地可直连）', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp-http-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  for (const transport of ['http', 'streamable_http']) {
    const artifact = makeZip({
      'connection.json': JSON.stringify({ transport, url: 'https://gw.example/api/mcp-gateway/relay/demo/stream' })
    })
    const res = await install(dataDir, 'mcp', `http-${transport}`, artifact)
    assert.equal(res.ok, true)
    const item = (readMcpServers(dataDir) as Array<Record<string, unknown>>).find((s) => s.name === `http-${transport}`)
    assert.equal(item?.kind, undefined)
    assert.equal(item?.transport, 'http')
    assert.equal(item?.url, 'https://gw.example/api/mcp-gateway/relay/demo/stream')
  }
})

test('installer mcp：http + 自定义 headers 记 market-remote（本地不透传认证头）', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp-http2-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const artifact = makeZip({
    'connection.json': JSON.stringify({
      transport: 'http',
      url: 'https://gw.example/stream',
      headers: { Authorization: 'Bearer secret' }
    })
  })
  const res = await install(dataDir, 'mcp', 'http-auth', artifact)
  assert.equal(res.ok, true)
  assert.ok(res.output.includes('market-remote'), res.output)
  const item = (readMcpServers(dataDir) as Array<Record<string, unknown>>).find((s) => s.name === 'http-auth')
  assert.equal(item?.kind, 'market-remote')
  assert.equal(item?.transport, undefined)
})

test('installer mcp：stdlib 本地 spawn 依赖 env/包内实现文件时标记 market-remote', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp3-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const artifact = makeZip({
    'connection.json': JSON.stringify({
      transport: 'stdio',
      command: 'python',
      args: ['implementation/demo.py'],
      env: { API_KEY: 'redacted' }
    })
  })
  const res = await install(dataDir, 'mcp', 'impl-cap', artifact)
  assert.equal(res.ok, true)
  const item = (readMcpServers(dataDir) as Array<Record<string, unknown>>).find((s) => s.name === 'impl-cap')
  assert.equal(item?.kind, 'market-remote')
})

test('installer mcp：sse + url + 无 headers 可记为 url 条目', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp4-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const artifact = makeZip({
    'connection.json': JSON.stringify({ transport: 'sse', url: 'http://10.0.0.5:9000/sse' })
  })
  const res = await install(dataDir, 'mcp', 'sse-cap', artifact)
  assert.equal(res.ok, true)
  const item = (readMcpServers(dataDir) as Array<Record<string, unknown>>).find((s) => s.name === 'sse-cap')
  assert.equal(item?.kind, undefined)
  assert.equal(item?.url, 'http://10.0.0.5:9000/sse')
})

test('installer mcp：包缺少 connection.json 时报错', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp5-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const res = await install(
    dataDir,
    'mcp',
    'broken',
    makeZip({ 'mcp.json': '{}', 'tools.json': '{}', 'security.json': '{}' })
  )
  assert.equal(res.ok, false)
  assert.ok(res.output.includes('connection.json'), res.output)
  assert.ok(!existsSync(mcpJsonPath(dataDir)), '失败不应写 mcp.json')
})

test('installer mcp：connection.json 非对象或 transport 不受支持时报错', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp6-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const badJson = await install(
    dataDir,
    'mcp',
    'bad-json',
    makeZip({ 'connection.json': 'not json' })
  )
  assert.equal(badJson.ok, false)
  assert.ok(badJson.output.includes('解析失败'), badJson.output)
  const badTransport = await install(
    dataDir,
    'mcp',
    'bad-tr',
    makeZip({ 'connection.json': JSON.stringify({ transport: 'carrier-pigeon' }) })
  )
  assert.equal(badTransport.ok, false)
  assert.ok(badTransport.output.includes('transport'), badTransport.output)
})

test('installer mcp：现有 mcp.json 损坏时报错且不改动原文件', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp7-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  writeMcpRaw(dataDir, '{broken json')
  const res = await install(
    dataDir,
    'mcp',
    'x',
    makeZip({ 'connection.json': JSON.stringify({ transport: 'stdio', command: 'python', args: [] }) })
  )
  assert.equal(res.ok, false)
  assert.equal(readFileSync(mcpJsonPath(dataDir), 'utf8'), '{broken json')
})

test('installer mcp：无既有 mcp.json 时新建', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp8-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const res = await install(
    dataDir,
    'mcp',
    'fresh',
    makeZip({ 'connection.json': JSON.stringify({ transport: 'stdio', command: 'uvx', args: ['demo'] }) })
  )
  assert.equal(res.ok, true)
  const servers = readMcpServers(dataDir) as Array<Record<string, unknown>>
  assert.equal(servers.length, 1)
  assert.equal(servers[0].name, 'fresh')
})

// ---------- mcp 双模式(平台桥接 / 本地安装) ----------

test('installer mcp platform 模式：输出标准占位符条目(transport:http + ${MARKET_URL}/${MARKET_TOKEN}) 且不解压实现', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp-plat-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  // stdio+implementation(自动模式会记 market-remote)与未知 transport 都应被 platform 模式覆盖
  const stdioImpl = makeZip({
    'connection.json': JSON.stringify({
      transport: 'stdio',
      command: 'python',
      args: ['implementation/server.py'],
      env: { TOKEN: '${TOKEN}' }
    }),
    'implementation/server.py': 'print(1)'
  })
  const res1 = await install(dataDir, 'mcp', 'plat-stdio', stdioImpl, { mode: 'platform' })
  assert.equal(res1.ok, true, res1.output)
  assert.ok(res1.output.includes('云端托管'), res1.output)
  assert.ok(res1.output.includes('运行时注入'), res1.output)
  const item1 = (readMcpServers(dataDir) as Array<Record<string, unknown>>).find((s) => s.name === 'plat-stdio')
  assert.equal(item1?.kind, undefined, 'platform 模式不再写 kind')
  assert.equal(item1?.command, undefined)
  assert.equal(item1?.env, undefined)
  assert.deepEqual(item1, {
    name: 'plat-stdio',
    source: 'capability: plat-stdio@1.0.0',
    transport: 'http',
    url: '${MARKET_URL}/api/mcp-gateway/relay/plat-stdio/stream',
    headers: { Authorization: 'Bearer ${MARKET_TOKEN}' }
  })
  assert.ok(
    !existsSync(join(dataDir, 'localagent', 'mcp-servers', 'plat-stdio')),
    'platform 模式不应解压实现文件'
  )
  const unknownTransport = makeZip({
    'connection.json': JSON.stringify({ transport: 'carrier-pigeon', command: 'x' })
  })
  const res2 = await install(dataDir, 'mcp', 'plat-unknown', unknownTransport, { mode: 'platform' })
  assert.equal(res2.ok, true, res2.output)
  const item2 = (readMcpServers(dataDir) as Array<Record<string, unknown>>).find((s) => s.name === 'plat-unknown')
  assert.deepEqual(item2, {
    name: 'plat-unknown',
    source: 'capability: plat-unknown@1.0.0',
    transport: 'http',
    url: '${MARKET_URL}/api/mcp-gateway/relay/plat-unknown/stream',
    headers: { Authorization: 'Bearer ${MARKET_TOKEN}' }
  })
})

test('installer mcp platform 模式：能力名做 URL 编码后进 url 路径', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp-plat-enc-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const res = await install(
    dataDir,
    'mcp',
    'weird#cap',
    makeZip({ 'connection.json': JSON.stringify({ transport: 'stdio', command: 'python' }) }),
    { mode: 'platform' }
  )
  assert.equal(res.ok, true, res.output)
  const item = (readMcpServers(dataDir) as Array<Record<string, unknown>>).find((s) => s.name === 'weird#cap')
  assert.equal(item?.url, '${MARKET_URL}/api/mcp-gateway/relay/weird%23cap/stream')
})

test('installer mcp platform 模式：包缺 connection.json 仍报错', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp-plat2-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const res = await install(dataDir, 'mcp', 'no-conn', makeZip({ 'mcp.json': '{}' }), { mode: 'platform' })
  assert.equal(res.ok, false)
  assert.ok(res.output.includes('connection.json'), res.output)
  assert.ok(!existsSync(mcpJsonPath(dataDir)), '失败不应写 mcp.json')
})

test('installer mcp local 模式：简单 stdio 原样写 command/args 条目', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp-loc1-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const artifact = makeZip({
    'connection.json': JSON.stringify({ transport: 'stdio', command: 'uvx', args: ['demo-mcp'] })
  })
  const res = await install(dataDir, 'mcp', 'uvx-cap', artifact, { mode: 'local' })
  assert.equal(res.ok, true, res.output)
  assert.ok(res.output.includes('本地安装'), res.output)
  const item = (readMcpServers(dataDir) as Array<Record<string, unknown>>).find((s) => s.name === 'uvx-cap')
  assert.equal(item?.kind, undefined)
  assert.equal(item?.command, 'uvx')
  assert.deepEqual(item?.args, ['demo-mcp'])
})

test('installer mcp local 模式：解压 implementation/** 并改写 args 为绝对路径(basename 递归兜底)、env 原样保留', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp-loc2-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const artifact = makeZip({
    'connection.json': JSON.stringify({
      transport: 'stdio',
      command: 'python',
      args: ['implementation/server.py', 'helper.py'],
      env: { TOKEN: '${TOKEN}', MODE: 'local' }
    }),
    'implementation/server.py': 'print("server")',
    'implementation/lib/helper.py': 'print("helper")'
  })
  const res = await install(dataDir, 'mcp', 'time-local', artifact, { mode: 'local' })
  assert.equal(res.ok, true, res.output)
  const item = (readMcpServers(dataDir) as Array<Record<string, unknown>>).find((s) => s.name === 'time-local')
  const dir = join(dataDir, 'localagent', 'mcp-servers', 'time-local')
  assert.ok(existsSync(join(dir, 'server.py')), 'implementation 文件应解压到 mcp-servers/<name>')
  assert.ok(existsSync(join(dir, 'lib', 'helper.py')), '嵌套实现文件保留相对目录结构')
  const args = item?.args as string[]
  assert.ok(isAbsolute(args[0]), `args 应改写为绝对路径: ${args[0]}`)
  assert.equal(args[0], join(dir, 'server.py'))
  assert.ok(existsSync(args[0]))
  assert.equal(args[1], join(dir, 'lib', 'helper.py'), '包内相对脚本应按 basename 递归唯一匹配')
  assert.ok(existsSync(args[1]))
  assert.equal(item?.cwd, dir, 'cwd 缺省指向解压目录')
  assert.deepEqual(item?.env, { TOKEN: '${TOKEN}', MODE: 'local' }, 'env 占位符本地不解析，原样写入')
})

test('installer mcp local 模式：gateway/缺 command/带 headers 的 http 包报「仅支持云端托管」', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp-loc3-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const conns: Array<Record<string, unknown>> = [
    { transport: 'gateway', server: 'gw-prod' },
    { transport: 'stdio' },
    { transport: 'http', url: 'https://gw.example/stream', headers: { Authorization: 'Bearer secret' } }
  ]
  for (const conn of conns) {
    const res = await install(
      dataDir,
      'mcp',
      `no-local-${String(conn.transport)}`,
      makeZip({ 'connection.json': JSON.stringify(conn) }),
      { mode: 'local' }
    )
    assert.equal(res.ok, false, `transport=${String(conn.transport)} 应失败`)
    assert.ok(res.output.includes('仅支持云端托管'), res.output)
  }
  assert.ok(!existsSync(mcpJsonPath(dataDir)), '失败不应写 mcp.json')
})

test('installer mcp local 模式：http 无 headers 写 url 条目', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp-loc4-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const artifact = makeZip({
    'connection.json': JSON.stringify({ transport: 'streamable_http', url: 'http://10.0.0.5:9000/stream' })
  })
  const res = await install(dataDir, 'mcp', 'http-local', artifact, { mode: 'local' })
  assert.equal(res.ok, true, res.output)
  const item = (readMcpServers(dataDir) as Array<Record<string, unknown>>).find((s) => s.name === 'http-local')
  assert.equal(item?.kind, undefined)
  assert.equal(item?.transport, 'http')
  assert.equal(item?.url, 'http://10.0.0.5:9000/stream')
})

test('uninstall mcp：同时清理本地安装解压目录 mcp-servers/<name>', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'un-mcp-local-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const artifact = makeZip({
    'connection.json': JSON.stringify({
      transport: 'stdio',
      command: 'python',
      args: ['implementation/server.py']
    }),
    'implementation/server.py': 'print(1)'
  })
  await install(dataDir, 'mcp', 'time-local', artifact, { mode: 'local' })
  const dir = join(dataDir, 'localagent', 'mcp-servers', 'time-local')
  assert.ok(existsSync(dir))
  const res = await uninstallCapability({ dataDir, type: 'mcp', name: 'time-local' })
  assert.equal(res.ok, true)
  assert.ok(!existsSync(dir), '卸载后本地安装文件目录应删除')
  const servers = readMcpServers(dataDir) as Array<Record<string, unknown>>
  assert.equal(servers.find((s) => s.name === 'time-local'), undefined)
})

// ---------- agent ----------

test('installer agent：agent.json + PROMPT.md(+TEAM.md) 落到本地人设目录', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-agent-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const artifact = makeZip({
    'agent.json': '{"name":"customer-bot","description":"客服"}',
    'PROMPT.md': '# 你是客服助手',
    'TEAM.md': '# 团队'
  })
  const res = await install(dataDir, 'agent', 'customer-bot', artifact, { version: '0.2.0' })
  assert.equal(res.ok, true)
  const dir = join(agentsDir(dataDir), 'customer-bot')
  assert.equal(readFileSync(join(dir, 'agent.json'), 'utf8'), '{"name":"customer-bot","description":"客服"}')
  assert.equal(readFileSync(join(dir, 'PROMPT.md'), 'utf8'), '# 你是客服助手')
  assert.equal(readFileSync(join(dir, 'TEAM.md'), 'utf8'), '# 团队')
  assert.ok(res.output.includes('customer-bot'), res.output)
})

test('installer agent：包缺少 PROMPT.md 时报错且不落目录', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-agent2-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const res = await install(dataDir, 'agent', 'customer-bot', makeZip({ 'agent.json': '{}' }))
  assert.equal(res.ok, false)
  assert.ok(res.output.includes('PROMPT.md'), res.output)
  assert.ok(!existsSync(join(agentsDir(dataDir), 'customer-bot')))
})

// ---------- tool ----------

test('installer tool：返回 ok:false 提示远程适配且不落盘', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-tool-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const res = await install(dataDir, 'tool', 'some-tool', makeZip({ 'tool.json': '{}' }))
  assert.equal(res.ok, false)
  assert.ok(res.output.includes('远程适配'), res.output)
  assert.ok(!existsSync(join(dataDir, 'localagent')), 'tool 不应产生任何本地目录')
})

// ---------- 名称安全 ----------

test('installer 拒绝路径分隔符/控制字符/Windows 非法字符的能力名(防逃逸与 raw ENOENT)', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-name-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  for (const badName of ['../evil', 'bad?name', 'bad\u0007name', 'bad*name']) {
    const res = await install(dataDir, 'skill', badName, makeZip({ 'SKILL.md': '# s' }))
    assert.equal(res.ok, false)
    assert.ok(res.output.includes('非法能力名'), res.output)
  }
  assert.ok(!existsSync(join(dataDir, 'localagent')))
})

// ---------- uninstall ----------

test('uninstall skill：删除技能目录', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'un-skill-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  await install(dataDir, 'skill', 'doc-check', makeZip({ 'SKILL.md': '# s' }))
  assert.ok(existsSync(join(skillsDir(dataDir), 'doc-check')))
  const res = await uninstallCapability({ dataDir, type: 'skill', name: 'doc-check' })
  assert.equal(res.ok, true)
  assert.ok(res.output.includes('卸载'), res.output)
  assert.ok(!existsSync(join(skillsDir(dataDir), 'doc-check')))
})

test('uninstall skill：未安装时幂等返回 ok:true', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'un-skill2-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const res = await uninstallCapability({ dataDir, type: 'skill', name: 'ghost' })
  assert.equal(res.ok, true)
  assert.ok(res.output.includes('未发现'), res.output)
})

test('uninstall mcp：仅移除同名条目并保留其它', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'un-mcp-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  writeMcpRaw(
    dataDir,
    JSON.stringify({
      servers: [
        { name: 'keep', command: 'npx' },
        { name: 'demo-mcp', kind: 'market-remote', source: 'capability: demo-mcp@1.0.0' }
      ]
    })
  )
  const res = await uninstallCapability({ dataDir, type: 'mcp', name: 'demo-mcp' })
  assert.equal(res.ok, true)
  const servers = readMcpServers(dataDir) as Array<Record<string, unknown>>
  assert.equal(servers.length, 1)
  assert.equal(servers[0].name, 'keep')
})

test('uninstall mcp：无对应条目时配置保持不变', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'un-mcp2-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const original = JSON.stringify({ servers: [{ name: 'keep', command: 'npx' }] })
  writeMcpRaw(dataDir, original)
  const res = await uninstallCapability({ dataDir, type: 'mcp', name: 'ghost' })
  assert.equal(res.ok, true)
  assert.ok(res.output.includes('未发现'), res.output)
  assert.equal(readFileSync(mcpJsonPath(dataDir), 'utf8'), original)
})

test('uninstall agent：删除本地人设目录', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'un-agent-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  await install(dataDir, 'agent', 'customer-bot', makeZip({ 'agent.json': '{}', 'PROMPT.md': '# p' }))
  assert.ok(existsSync(join(agentsDir(dataDir), 'customer-bot')))
  const res = await uninstallCapability({ dataDir, type: 'agent', name: 'customer-bot' })
  assert.equal(res.ok, true)
  assert.ok(!existsSync(join(agentsDir(dataDir), 'customer-bot')))
})

test('uninstall tool：返回 ok:false 提示远程摘除', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'un-tool-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const res = await uninstallCapability({ dataDir, type: 'tool', name: 'some-tool' })
  assert.equal(res.ok, false)
  assert.ok(res.output.includes('远程'), res.output)
})

test('shouldRefuseRemoteInstall：remote skill/agent 拒绝本地安装，提示加入即可使用', () => {
  const skill = shouldRefuseRemoteInstall({ type: 'skill', distribution: 'remote' })
  assert.ok(skill && skill.includes('加入即可使用'), String(skill))
  const agent = shouldRefuseRemoteInstall({ type: 'agent', distribution: 'remote' })
  assert.ok(agent && agent.includes('加入即可使用'), String(agent))
})

test('shouldRefuseRemoteInstall：remote mcp 仅放行云端托管（mode 缺省/local 均拒绝）', () => {
  const noMode = shouldRefuseRemoteInstall({ type: 'mcp', distribution: 'remote' })
  assert.ok(noMode && noMode.includes('云端托管'), String(noMode))
  const local = shouldRefuseRemoteInstall({ type: 'mcp', distribution: 'remote' }, 'local')
  assert.ok(local && local.includes('云端托管'), String(local))
  assert.equal(shouldRefuseRemoteInstall({ type: 'mcp', distribution: 'remote' }, 'platform'), null)
})

test('shouldRefuseRemoteInstall：本地/双模式能力与 remote tool 放行', () => {
  assert.equal(shouldRefuseRemoteInstall({ type: 'skill', distribution: 'local' }), null)
  assert.equal(shouldRefuseRemoteInstall({ type: 'agent', distribution: 'both' }), null)
  assert.equal(shouldRefuseRemoteInstall({ type: 'skill' }), null)
  assert.equal(shouldRefuseRemoteInstall({ type: 'tool', distribution: 'remote' }), null)
})

test('shouldRefuseRemoteInstall：runtime cloud-only 拒绝本地安装（mcp 缺省/local 均拒，platform 放行）', () => {
  const cloudOnly = { cloud: true, local: false, recommended: 'cloud' as const }
  for (const type of ['skill', 'agent'] as const) {
    const res = shouldRefuseRemoteInstall({ type, runtime: cloudOnly })
    assert.ok(res && res.includes('加入即可使用'), String(res))
  }
  // 旧 distribution 缺失也不影响：runtime 优先
  assert.ok(shouldRefuseRemoteInstall({ type: 'mcp', runtime: cloudOnly }))
  assert.ok(shouldRefuseRemoteInstall({ type: 'mcp', runtime: cloudOnly }, 'local'))
  assert.equal(shouldRefuseRemoteInstall({ type: 'mcp', runtime: cloudOnly }, 'platform'), null)
  assert.equal(shouldRefuseRemoteInstall({ type: 'tool', runtime: cloudOnly }), null)
})

test('shouldRefuseRemoteInstall：runtime local-only 拒绝云端托管（本地安装放行）', () => {
  const localOnly = { cloud: false, local: true, recommended: 'local' as const }
  const refused = shouldRefuseRemoteInstall({ type: 'mcp', runtime: localOnly }, 'platform')
  assert.ok(refused && refused.includes('仅支持本地安装'), String(refused))
  assert.equal(shouldRefuseRemoteInstall({ type: 'mcp', runtime: localOnly }, 'local'), null)
  assert.equal(shouldRefuseRemoteInstall({ type: 'mcp', runtime: localOnly }), null)
  assert.equal(shouldRefuseRemoteInstall({ type: 'skill', runtime: localOnly }), null)
  assert.equal(shouldRefuseRemoteInstall({ type: 'agent', runtime: localOnly }), null)
})

test('shouldRefuseRemoteInstall：runtime 双可放行；runtime 缺失/空对象/单维缺省回退 distribution', () => {
  const both = { cloud: true, local: true, recommended: 'cloud' as const }
  assert.equal(shouldRefuseRemoteInstall({ type: 'skill', runtime: both }), null)
  assert.equal(shouldRefuseRemoteInstall({ type: 'mcp', runtime: both }, 'local'), null)
  assert.equal(shouldRefuseRemoteInstall({ type: 'mcp', runtime: both }, 'platform'), null)
  // 空 runtime / 非法 runtime → 回退 distribution（与旧行为一致）
  assert.ok(shouldRefuseRemoteInstall({ type: 'skill', distribution: 'remote', runtime: {} }))
  assert.equal(shouldRefuseRemoteInstall({ type: 'skill', distribution: 'both', runtime: {} }), null)
  assert.equal(shouldRefuseRemoteInstall({ type: 'mcp', distribution: 'local', runtime: {} }, 'platform'), null)
  // 单维 runtime：local=false 拒本地，另一维按 distribution 回退
  assert.ok(shouldRefuseRemoteInstall({ type: 'skill', runtime: { local: false } }))
  assert.equal(shouldRefuseRemoteInstall({ type: 'mcp', runtime: { cloud: false }, distribution: 'both' }, 'local'), null)
  assert.ok(shouldRefuseRemoteInstall({ type: 'mcp', runtime: { cloud: false }, distribution: 'both' }, 'platform'))
})

function errnoError(code: string): NodeJS.ErrnoException {
  const err = new Error(`${code}: simulated`) as NodeJS.ErrnoException
  err.code = code
  return err
}

test('removeDirWithRetry：EPERM 重试后成功（退避等待被注入桩跳过）', async () => {
  let calls = 0
  const res = await removeDirWithRetry('C:\\fake\\dir', {
    rmImpl: () => {
      calls += 1
      if (calls < 3) throw errnoError('EPERM')
    },
    sleepImpl: () => {}
  })
  assert.equal(res.ok, true)
  assert.equal(calls, 3)
})

test('removeDirWithRetry：持续 EPERM 返回 ok:false 且不抛错', async () => {
  let calls = 0
  const res = await removeDirWithRetry('C:\\fake\\dir', {
    attempts: 3,
    rmImpl: () => {
      calls += 1
      throw errnoError('EPERM')
    },
    sleepImpl: () => {}
  })
  assert.equal(res.ok, false)
  assert.ok(res.error?.includes('EPERM'), res.error)
  assert.equal(calls, 3)
})

test('removeDirWithRetry：ENOTFOUND/ENOENT 视为成功且不重试', async () => {
  let notFoundCalls = 0
  const notFound = await removeDirWithRetry('C:\\fake\\dir', {
    rmImpl: () => {
      notFoundCalls += 1
      throw errnoError('ENOTFOUND')
    },
    sleepImpl: () => {}
  })
  assert.equal(notFound.ok, true)
  assert.equal(notFoundCalls, 1)

  let enoentCalls = 0
  const enoent = await removeDirWithRetry('C:\\fake\\dir', {
    rmImpl: () => {
      enoentCalls += 1
      throw errnoError('ENOENT')
    },
    sleepImpl: () => {}
  })
  assert.equal(enoent.ok, true)
  assert.equal(enoentCalls, 1)
})

test('uninstall mcp：目录删除失败仍移除配置条目并附中文提示', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'un-mcp-busy-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  writeMcpRaw(
    dataDir,
    JSON.stringify({
      servers: [
        { name: 'busy-mcp', command: 'npx', env: { A: '1' } },
        { name: 'keep-mcp', command: 'npx' }
      ]
    })
  )
  const localDir = join(dataDir, 'localagent', 'mcp-servers', 'busy-mcp')
  mkdirSync(localDir, { recursive: true })
  writeFileSync(join(localDir, 'server.py'), 'print(1)')

  const res = await uninstallCapability({
    dataDir,
    type: 'mcp',
    name: 'busy-mcp',
    removeDirImpl: async () => ({ ok: false, error: 'EPERM: simulated lock' })
  })
  assert.equal(res.ok, true)
  assert.ok(res.output.includes('已移除 MCP 配置条目 busy-mcp'), res.output)
  assert.ok(res.output.includes('本地文件被占用未能删除'), res.output)
  assert.ok(res.output.includes('重启 dashboard 后可重试/手动清理'), res.output)
  const servers = readMcpServers(dataDir) as Array<Record<string, unknown>>
  assert.ok(!servers.some((s) => s.name === 'busy-mcp'))
  assert.ok(servers.some((s) => s.name === 'keep-mcp'), '其它条目应保留')
})

// ---------- mcp: 标准 server.json ----------

test('installer mcp：标准 server.json(remotes) 可本地安装', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp-std-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const artifact = makeZip({
    'server.json': JSON.stringify({
      name: 'io.github.xzrobot/demo',
      version: '1.0.0',
      description: 'demo',
      remotes: [{ type: 'streamable-http', url: 'https://mcp.example.com/mcp' }]
    })
  })
  const res = await install(dataDir, 'mcp', 'demo-std', artifact, { mode: 'local' })
  assert.equal(res.ok, true, res.output)
  const servers = readMcpServers(dataDir) as Array<Record<string, unknown>>
  const entry = servers.find((s) => s.name === 'demo-std') as Record<string, unknown> | undefined
  assert.ok(entry)
  assert.equal(entry.url, 'https://mcp.example.com/mcp')
  assert.equal(entry.transport, 'http')
})

test('installer mcp：标准 server.json(仅 packages) 本地安装提示云端托管', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp-pkg-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const artifact = makeZip({
    'server.json': JSON.stringify({
      name: 'io.github.xzrobot/npm-only',
      version: '1.0.0',
      packages: [{ registry: 'npm', identifier: 'npm-only-mcp' }]
    })
  })
  const res = await install(dataDir, 'mcp', 'npm-only', artifact, { mode: 'local' })
  assert.equal(res.ok, false)
  assert.ok(res.output.includes('云端托管'), res.output)
})

test('installer mcp：本地 stdio 需命令确认(返回预览, 未确认不写配置)', async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'inst-mcp-consent-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const artifact = makeZip({
    'connection.json': JSON.stringify({ transport: 'stdio', command: 'npx', args: ['-y', 'some-mcp'] })
  })
  const first = await install(dataDir, 'mcp', 'consent-mcp', artifact, {
    mode: 'local',
    confirmed: false
  })
  assert.equal(first.ok, false)
  assert.ok(first.consent)
  assert.equal(first.consent?.command, 'npx')
  assert.deepEqual(first.consent?.args, ['-y', 'some-mcp'])
  // 未确认时不得写入本地 MCP 配置
  assert.equal(existsSync(mcpJsonPath(dataDir)), false)

  const second = await install(dataDir, 'mcp', 'consent-mcp', artifact, {
    mode: 'local',
    confirmed: true
  })
  assert.equal(second.ok, true, second.output)
  const servers = readMcpServers(dataDir) as Array<Record<string, unknown>>
  assert.ok(servers.some((s) => s.name === 'consent-mcp' && s.command === 'npx'))
})
