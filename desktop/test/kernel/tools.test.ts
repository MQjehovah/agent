import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { builtinTools, formatKbResults, kbSearchTool } from '../../electron/main/kernel/tools'
import type { ToolContext, ToolDefinition } from '../../electron/main/kernel/types'

const ws = mkdtempSync(join(tmpdir(), 'ktest-'))
const ctx: ToolContext = { workspace: ws, sessionId: 's1' }

/** 按名称取工具，缺失直接报错，避免每个用例重复 filter */
function tool(name: string): ToolDefinition {
  const t = builtinTools.find(t => t.name === name)
  if (!t) throw new Error(`缺少内置工具: ${name}`)
  return t
}

test('file_write/read roundtrip', async () => {
  const w = await tool('file_write').execute({ path: 'a.txt', content: 'hello' }, ctx)
  assert.ok(w.ok, w.output)
  const r = await tool('file_read').execute({ path: 'a.txt' }, ctx)
  assert.ok(r.ok, r.output)
  assert.equal(r.output, 'hello')
})

test('file_read honors offset and limit', async () => {
  writeFileSync(join(ws, 'lines.txt'), Array.from({ length: 10 }, (_, i) => `line${i + 1}`).join('\n'))
  const res = await tool('file_read').execute({ path: 'lines.txt', offset: 2, limit: 3 }, ctx)
  assert.ok(res.ok, res.output)
  assert.equal(res.output, 'line2\nline3\nline4')
})

test('file_read missing file returns ok:false instead of throwing', async () => {
  const res = await tool('file_read').execute({ path: 'ghost.txt' }, ctx)
  assert.ok(!res.ok)
})

test('file_write creates parent directories', async () => {
  const res = await tool('file_write').execute({ path: 'deep/nested/c.txt', content: 'x' }, ctx)
  assert.ok(res.ok, res.output)
  assert.equal(readText('deep/nested/c.txt'), 'x')
})

test('file_edit replaces exact match', async () => {
  writeFileSync(join(ws, 'b.txt'), 'foo bar')
  const res = await tool('file_edit').execute({ path: 'b.txt', old: 'bar', new: 'baz' }, ctx)
  assert.ok(res.ok, res.output)
  assert.equal(readText('b.txt'), 'foo baz')
})

test('file_edit all:true replaces every occurrence', async () => {
  writeFileSync(join(ws, 'all.txt'), 'x x x')
  const res = await tool('file_edit').execute({ path: 'all.txt', old: 'x', new: 'y', all: true }, ctx)
  assert.ok(res.ok, res.output)
  assert.equal(readText('all.txt'), 'y y y')
})

test('file_edit without match fails', async () => {
  const res = await tool('file_edit').execute({ path: 'b.txt', old: 'nope', new: 'x' }, ctx)
  assert.ok(!res.ok)
})

test('file_write outside workspace rejected', async () => {
  const res = await tool('file_write').execute({ path: join(ws, '..', 'evil.txt'), content: 'x' }, ctx)
  assert.ok(!res.ok)
  assert.ok(!existsSync(join(ws, '..', 'evil.txt')))
})

test('file_write dotdot escape rejected', async () => {
  const res = await tool('file_write').execute({ path: 'a/../../evil2.txt', content: 'x' }, ctx)
  assert.ok(!res.ok)
})

test('file_write/read/edit through junction to outside dir rejected', async () => {
  // 目录联接（junction）在 Windows 无需管理员权限；POSIX 用目录符号链接等价覆盖
  const outside = mkdtempSync(join(tmpdir(), 'kout-'))
  const link = join(ws, 'link-out')
  symlinkSync(outside, link, process.platform === 'win32' ? 'junction' : 'dir')
  writeFileSync(join(outside, 'secret.txt'), 'secret')
  try {
    const w = await tool('file_write').execute({ path: 'link-out/evil.txt', content: 'x' }, ctx)
    assert.ok(!w.ok, '经符号链接写入外部目录应被拒绝')
    assert.ok(!existsSync(join(outside, 'evil.txt')), '外部目录不应产生文件')

    const r = await tool('file_read').execute({ path: 'link-out/secret.txt' }, ctx)
    assert.ok(!r.ok, '经符号链接读取外部文件应被拒绝')

    const e = await tool('file_edit').execute({ path: 'link-out/secret.txt', old: 's', new: 'S' }, ctx)
    assert.ok(!e.ok, '经符号链接编辑外部文件应被拒绝')
    assert.equal(readText('secret.txt', outside), 'secret', '外部文件内容不应被修改')
  } finally {
    rmSync(link, { recursive: true, force: true })
    rmSync(outside, { recursive: true, force: true })
  }
})

test('glob matches recursive pattern and skips node_modules', async () => {
  mkdirSync(join(ws, 'src'), { recursive: true })
  mkdirSync(join(ws, 'docs'), { recursive: true })
  mkdirSync(join(ws, 'node_modules', 'pkg'), { recursive: true })
  writeFileSync(join(ws, 'src', 'a.ts'), 'hello world')
  writeFileSync(join(ws, 'docs', 'b.md'), 'hello again')
  writeFileSync(join(ws, 'node_modules', 'pkg', 'n.js'), 'x')

  const res = await tool('glob').execute({ pattern: '**/*.ts' }, ctx)
  assert.ok(res.ok, res.output)
  assert.ok(res.output.split('\n').includes('src/a.ts'), res.output)

  const js = await tool('glob').execute({ pattern: '**/*.js' }, ctx)
  assert.ok(js.ok, js.output)
  assert.ok(!js.output.includes('node_modules'), 'node_modules 应默认排除')
})

test('grep finds matches with relative path and line number', async () => {
  const res = await tool('grep').execute({ pattern: 'hello' }, ctx)
  assert.ok(res.ok, res.output)
  assert.ok(res.output.includes('src/a.ts:1: hello world'), res.output)
  assert.ok(res.output.includes('docs/b.md:1: hello again'), res.output)
})

test('grep include filter limits file scope', async () => {
  const res = await tool('grep').execute({ pattern: 'hello', include: '*.md' }, ctx)
  assert.ok(res.ok, res.output)
  assert.ok(res.output.includes('docs/b.md'), res.output)
  assert.ok(!res.output.includes('src/a.ts'), res.output)
})

test('grep invalid regex falls back to literal search', async () => {
  const res = await tool('grep').execute({ pattern: '[' }, ctx)
  assert.ok(res.ok, res.output)
  assert.equal(res.output, '(无匹配)')
})

test('terminal echoes output', async () => {
  const res = await tool('terminal').execute({ command: 'echo ok' }, ctx)
  assert.ok(res.ok, res.output)
  assert.ok(res.output.includes('ok'), res.output)
})

test('terminal non-zero exit returns ok:false', async () => {
  const res = await tool('terminal').execute({ command: 'exit 3' }, ctx)
  assert.ok(!res.ok)
})

test('kb_search sends query to rag and folds hits into readable output', async () => {
  const calls: Array<{ path: string; body: { query?: string; top_k?: number } }> = []
  const kb = kbSearchTool(async (path, body) => {
    calls.push({ path, body: body as { query?: string; top_k?: number } })
    return {
      results: [
        { id: '1', title: '报销制度', content: '差旅报销需附发票与行程单', source: '制度文档' },
        { id: '2', title: '采购付款', content: '付款前须完成验收', source: 'wiki' }
      ],
      total: 2,
      graph_expanded: false
    }
  })
  const res = await kb.execute({ query: '报销制度' }, ctx)
  assert.ok(res.ok, res.output)
  assert.deepEqual(calls, [{ path: '/api/search', body: { query: '报销制度', top_k: 5 } }])
  assert.ok(res.output.includes('报销制度'), res.output)
  assert.ok(res.output.includes('差旅报销需附发票与行程单'), res.output)
  assert.ok(res.output.includes('【制度文档】'), res.output)
})

test('kb_search empty results reports no match and still ok', async () => {
  const kb = kbSearchTool(async () => ({ results: [], total: 0 }))
  const res = await kb.execute({ query: '不存在的制度' }, ctx)
  assert.ok(res.ok, res.output)
  assert.ok(res.output.includes('知识库无相关结果'), res.output)
})

test('kb_search folds rag exceptions into ok:false without throwing', async () => {
  const kb = kbSearchTool(async () => {
    throw new Error('连接被拒绝')
  })
  const res = await kb.execute({ query: '报销' }, ctx)
  assert.ok(!res.ok)
  assert.ok(res.output.includes('连接被拒绝'), res.output)
})

test('kb_search requires query param', async () => {
  const kb = kbSearchTool(async () => ({ results: [] }))
  const res = await kb.execute({}, ctx)
  assert.ok(!res.ok)
})

test('kb_search clamps top_k into 1..10', async () => {
  const bodies: unknown[] = []
  const kb = kbSearchTool(async (_path, body) => {
    bodies.push(body)
    return { results: [] }
  })
  await kb.execute({ query: 'q', top_k: 99 }, ctx)
  await kb.execute({ query: 'q', top_k: 0 }, ctx)
  await kb.execute({ query: 'q', top_k: -5 }, ctx)
  await kb.execute({ query: 'q', top_k: 3.7 }, ctx)
  assert.deepEqual(bodies, [
    { query: 'q', top_k: 10 },
    { query: 'q', top_k: 1 },
    { query: 'q', top_k: 1 },
    { query: 'q', top_k: 3 }
  ])
})

test('kb_search tolerates missing or non-array results', async () => {
  const missing = kbSearchTool(async () => ({ total: 0 }))
  const ra = await missing.execute({ query: 'q' }, ctx)
  assert.ok(ra.ok, ra.output)
  assert.ok(ra.output.includes('知识库无相关结果'), ra.output)

  const malformed = kbSearchTool(async () => ({ results: 'oops' }))
  const rb = await malformed.execute({ query: 'q' }, ctx)
  assert.ok(rb.ok, rb.output)
  assert.ok(rb.output.includes('知识库无相关结果'), rb.output)
})

test('formatKbResults drops stub hits without title or content', () => {
  const text = formatKbResults({ results: [{ id: '1', source: '制度文档' }, { id: '2' }] })
  assert.equal(text, '知识库无相关结果')
})

test('formatKbResults truncates oversized payloads to about 4KB', () => {
  const text = formatKbResults({
    results: [{ id: '1', title: '长文制度', content: '甲'.repeat(6000), source: '制度文档' }]
  })
  assert.ok(text.length < 5000, `output too long: ${text.length}`)
  assert.ok(text.includes('已截断'), text)
})

/** 以工作区相对路径读文本的小工具，便于断言落盘内容 */
function readText(rel: string, base = ws): string {
  return readFileSync(join(base, ...rel.split('/')), 'utf-8')
}
