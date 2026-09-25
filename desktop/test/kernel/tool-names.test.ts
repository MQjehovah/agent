import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildToolNameMaps, toProviderToolName } from '../../electron/main/kernel/tool-names'

const LEGAL = /^[a-zA-Z0-9_-]+$/
const LEGAL_MAX_64 = /^[a-zA-Z0-9_-]{1,64}$/

test('tool-names: toProviderToolName 点号/中文/空格/slash 均替换为下划线', () => {
  assert.equal(toProviderToolName('file.read'), 'file_read')
  assert.equal(toProviderToolName('file write'), 'file_write')
  assert.equal(toProviderToolName('a/b:c'), 'a_b_c')
  // mcp + 2 个原有下划线 + 4 个中文 + 2 个原有下划线 + 3 个中文 = mcp + 11 个下划线
  assert.equal(toProviderToolName('mcp__设备运维__列出表'), 'mcp' + '_'.repeat(11))
})

test('tool-names: 合法字符原样保留（字母/数字/下划线/连字符）', () => {
  assert.equal(toProviderToolName('kb_search'), 'kb_search')
  assert.equal(toProviderToolName('tool-2'), 'tool-2')
  assert.equal(toProviderToolName('A1_b-2'), 'A1_b-2')
})

test('tool-names: 空串回退 tool，且结果始终满足 provider 合法字符', () => {
  assert.equal(toProviderToolName(''), 'tool')
  for (const name of ['', 'file.read', '中文名', 'a b/c:d+e']) {
    assert.match(toProviderToolName(name), LEGAL, name)
  }
})

test('tool-names: buildToolNameMaps 合法名恒等且双向映射一致', () => {
  const { toProvider, toLocal } = buildToolNameMaps(['file_read', 'glob', 'mcp__fs__read'])
  assert.equal(toProvider.get('file_read'), 'file_read')
  assert.equal(toProvider.get('glob'), 'glob')
  assert.equal(toProvider.get('mcp__fs__read'), 'mcp__fs__read')
  assert.equal(toLocal.get('file_read'), 'file_read')
  assert.equal(toLocal.get('glob'), 'glob')
  assert.equal(toLocal.get('mcp__fs__read'), 'mcp__fs__read')
})

test('tool-names: 非法名冲突时追加 _2/_3 去重且反向映射指回原工具', () => {
  const { toProvider, toLocal } = buildToolNameMaps(['a.b', 'a_b', 'a b'])
  assert.equal(toProvider.get('a.b'), 'a_b')
  assert.equal(toProvider.get('a_b'), 'a_b_2')
  assert.equal(toProvider.get('a b'), 'a_b_3')
  assert.equal(toLocal.get('a_b'), 'a.b')
  assert.equal(toLocal.get('a_b_2'), 'a_b')
  assert.equal(toLocal.get('a_b_3'), 'a b')
})

test('tool-names: 中文 MCP 名与点号名映射后合法且可反解', () => {
  const names = ['file.read', 'mcp__设备运维__列出表', 'mcp__fs__read']
  const { toProvider, toLocal } = buildToolNameMaps(names)
  for (const name of names) {
    const mapped = toProvider.get(name)
    assert.ok(mapped, name)
    assert.match(mapped!, LEGAL, name)
    assert.equal(toLocal.get(mapped!), name)
  }
  const providers = names.map(n => toProvider.get(n)!)
  assert.equal(new Set(providers).size, names.length, 'provider 名必须互不重复')
})

test('tool-names: 空列表返回空映射', () => {
  const { toProvider, toLocal } = buildToolNameMaps([])
  assert.equal(toProvider.size, 0)
  assert.equal(toLocal.size, 0)
})

test('tool-names: provider 名长度上限 64，超出截断且仍合法', () => {
  const longLegal = 'a'.repeat(80)
  assert.equal(toProviderToolName(longLegal), 'a'.repeat(64))
  // 非法字符先替换再截断
  assert.equal(toProviderToolName('中'.repeat(100)), '_'.repeat(64))
  assert.match(toProviderToolName(longLegal), LEGAL_MAX_64)
  assert.match(toProviderToolName('中'.repeat(100)), LEGAL_MAX_64)
})

test('tool-names: 截断后冲突仍去重且不超长', () => {
  const a = 'a'.repeat(70) + '.x'
  const b = 'a'.repeat(70) + '.y'
  const { toProvider, toLocal } = buildToolNameMaps([a, b])
  const pa = toProvider.get(a)!
  const pb = toProvider.get(b)!
  assert.equal(pa, 'a'.repeat(64))
  assert.notEqual(pa, pb)
  assert.match(pb, LEGAL_MAX_64)
  assert.equal(toLocal.get(pa), a)
  assert.equal(toLocal.get(pb), b)
})
