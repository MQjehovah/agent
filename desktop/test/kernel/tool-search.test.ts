import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  SEARCH_TOOLS_DEFAULT_LIMIT,
  SEARCH_TOOLS_MAX_LIMIT,
  TOOL_SEARCH_THRESHOLD,
  clampLimit,
  connectorOf,
  createSearchToolsTool,
  formatSearchResult,
  isRemoteTool,
  normalizeToolSearchMode,
  progressiveHint,
  searchRemoteTools,
  selectRoundTools,
  shouldUseProgressive,
  type ToolSearchEntry
} from '../../electron/main/kernel/tool-search'
import type { ToolContext } from '../../electron/main/kernel/types'

const CTX: ToolContext = { workspace: 'w', sessionId: 'local-00000001' }

const ENTRIES: ToolSearchEntry[] = [
  { name: 'mcp__remote_terminal__connect_terminal', description: '连接远程终端' },
  { name: 'mcp__remote_terminal__run_command', description: '在远程终端执行命令' },
  { name: 'mcp__device_ops__restart_device', description: '重启设备' },
  { name: 'market:market_search', description: '搜索市场能力' },
  { name: 'file_read', description: '读取本地文件' }
]

test('tool-search: isRemoteTool 按 mcp__ / market: 前缀判定远程工具', () => {
  assert.equal(isRemoteTool('mcp__remote_terminal__connect_terminal'), true)
  assert.equal(isRemoteTool('market:market_search'), true)
  assert.equal(isRemoteTool('file_read'), false)
  assert.equal(isRemoteTool('search_tools'), false)
  assert.equal(isRemoteTool('kb_search'), false)
  assert.equal(isRemoteTool(''), false)
})

test('tool-search: connectorOf 解析所属连接器（无法判定回空串）', () => {
  assert.equal(connectorOf('mcp__remote_terminal__connect_terminal'), 'remote_terminal')
  assert.equal(connectorOf('mcp__device_ops__重启设备'), 'device_ops')
  assert.equal(connectorOf('market:market_search'), 'market')
  assert.equal(connectorOf('file_read'), '')
  assert.equal(connectorOf('mcp__novalue'), '')
})

test('tool-search: normalizeToolSearchMode 非法值回退 auto', () => {
  assert.equal(normalizeToolSearchMode('auto'), 'auto')
  assert.equal(normalizeToolSearchMode('always'), 'always')
  assert.equal(normalizeToolSearchMode('off'), 'off')
  assert.equal(normalizeToolSearchMode('banana'), 'auto')
  assert.equal(normalizeToolSearchMode(undefined), 'auto')
  assert.equal(normalizeToolSearchMode(42), 'auto')
})

test('tool-search: shouldUseProgressive 三模式 × 阈值边界(40/41)', () => {
  assert.equal(shouldUseProgressive('off', 0), false)
  assert.equal(shouldUseProgressive('off', 40), false)
  assert.equal(shouldUseProgressive('off', 41), false)
  assert.equal(shouldUseProgressive('off', 100), false)

  assert.equal(shouldUseProgressive('always', 0), true)
  assert.equal(shouldUseProgressive('always', 1), true)

  assert.equal(shouldUseProgressive('auto', 0), false)
  assert.equal(shouldUseProgressive('auto', 40), false)
  assert.equal(shouldUseProgressive('auto', 41), true)
  assert.equal(shouldUseProgressive('auto', 100), true)

  // 阈值可覆盖（测试注入用）
  assert.equal(shouldUseProgressive('auto', 5, 5), false)
  assert.equal(shouldUseProgressive('auto', 6, 5), true)
  assert.equal(TOOL_SEARCH_THRESHOLD, 40)
})

test('tool-search: searchRemoteTools 名称/描述/连接器不区分大小写匹配', () => {
  // 名称命中（大写 query）
  const byName = searchRemoteTools(ENTRIES, 'CONNECT_TERMINAL')
  assert.deepEqual(byName.map((h) => h.name), ['mcp__remote_terminal__connect_terminal'])
  // 连接器命中（remote_terminal 出现在连接器与名称里）
  const byConnector = searchRemoteTools(ENTRIES, 'remote_terminal')
  assert.deepEqual(
    byConnector.map((h) => h.name).sort(),
    ['mcp__remote_terminal__connect_terminal', 'mcp__remote_terminal__run_command']
  )
  // 描述命中（中文）
  const byDesc = searchRemoteTools(ENTRIES, '重启')
  assert.deepEqual(byDesc.map((h) => h.name), ['mcp__device_ops__restart_device'])
  // 无命中返回空（检索源本身由调用方限定为远程工具）
  assert.deepEqual(searchRemoteTools([ENTRIES[0]], '本地文件'), [])
})

test('tool-search: searchRemoteTools 排序(名称 > 连接器 > 描述)与同分按名称升序', () => {
  const entries: ToolSearchEntry[] = [
    { name: 'mcp__alpha__thing', description: '普通描述' },
    { name: 'mcp__beta__needle', description: '普通描述' },
    { name: 'mcp__needle__other', description: '普通描述' },
    { name: 'mcp__gamma__other', description: 'needle 在描述里' }
  ]
  const hits = searchRemoteTools(entries, 'needle')
  // 名称精确命中分最高；名称包含其次（连接器/描述也含 needle 的名字加分更多）
  assert.equal(hits[0].name, 'mcp__needle__other')
  // needle 同时命中名称与连接器 → 高于仅名称的 beta
  assert.equal(hits[1].name, 'mcp__beta__needle')
  // 仅描述命中排最后
  assert.equal(hits[hits.length - 1].name, 'mcp__gamma__other')
  // 同分按名称升序：构造两条仅描述命中的条目
  const tie = searchRemoteTools(
    [
      { name: 'mcp__z__a', description: '关键字' },
      { name: 'mcp__a__b', description: '关键字' }
    ],
    '关键字'
  )
  assert.deepEqual(tie.map((h) => h.name), ['mcp__a__b', 'mcp__z__a'])
})

test('tool-search: searchRemoteTools 空查询/空白查询返回空数组', () => {
  assert.deepEqual(searchRemoteTools(ENTRIES, ''), [])
  assert.deepEqual(searchRemoteTools(ENTRIES, '   '), [])
  assert.deepEqual(searchRemoteTools(ENTRIES, '\t\n'), [])
})

test('tool-search: searchRemoteTools limit 缺省 8，夹紧 1..20', () => {
  const many: ToolSearchEntry[] = Array.from({ length: 30 }, (_, i) => ({
    name: `mcp__srv__tool_${String(i).padStart(2, '0')}`,
    description: '通用工具'
  }))
  assert.equal(searchRemoteTools(many, 'tool').length, SEARCH_TOOLS_DEFAULT_LIMIT)
  assert.equal(searchRemoteTools(many, 'tool', 100).length, SEARCH_TOOLS_MAX_LIMIT)
  assert.equal(searchRemoteTools(many, 'tool', 0).length, 1)
  assert.equal(searchRemoteTools(many, 'tool', -5).length, 1)
  assert.equal(searchRemoteTools(many, 'tool', 3).length, 3)
  assert.equal(clampLimit(undefined), SEARCH_TOOLS_DEFAULT_LIMIT)
  assert.equal(clampLimit(0), 1)
  assert.equal(clampLimit(999), SEARCH_TOOLS_MAX_LIMIT)
})

test('tool-search: formatSearchResult 输出名称 —— [连接器 x] 描述，并说明已激活', () => {
  const hits = searchRemoteTools(ENTRIES, 'connect_terminal')
  const text = formatSearchResult(hits)
  assert.match(text, /已激活，下一轮对话可直接调用/)
  assert.match(text, /mcp__remote_terminal__connect_terminal —— \[连接器 remote_terminal\] 连接远程终端/)
})

test('tool-search: progressiveHint 空激活集写「无」，非空写列表', () => {
  const empty = progressiveHint([])
  assert.match(empty, /已激活：无/)
  assert.match(empty, /search_tools/)
  assert.match(empty, /先搜索再调用/)
  const some = progressiveHint(['mcp__a__x', 'market:b'])
  assert.match(some, /已激活：mcp__a__x、market:b/)
  assert.ok(!some.includes('已激活：无'))
})

test('tool-search: selectRoundTools 非渐进=全量；渐进=非远程 + 已激活(未注册的激活名被忽略)', () => {
  const wrap = (name: string) => ({ type: 'function' as const, function: { name, description: '', parameters: {} } })
  const all = [
    wrap('file_read'),
    wrap('search_tools'),
    wrap('mcp__remote_terminal__connect_terminal'),
    wrap('market:market_search')
  ]
  assert.deepEqual(
    selectRoundTools(all, false, []).map((t) => t.function.name),
    all.map((t) => t.function.name)
  )
  assert.deepEqual(
    selectRoundTools(all, true, []).map((t) => t.function.name),
    ['file_read', 'search_tools']
  )
  assert.deepEqual(
    selectRoundTools(all, true, ['mcp__remote_terminal__connect_terminal', 'mcp__gone__x']).map(
      (t) => t.function.name
    ),
    ['file_read', 'search_tools', 'mcp__remote_terminal__connect_terminal']
  )
})

test('tool-search: createSearchToolsTool 空查询拒绝，不触发激活', async () => {
  let activated: string[] = []
  const tool = createSearchToolsTool({
    listRemote: () => ENTRIES,
    activate: (names) => {
      activated = names
      return names
    }
  })
  const res = await tool.execute({ query: '   ' }, CTX)
  assert.equal(res.ok, false)
  assert.match(res.output, /query/)
  assert.deepEqual(activated, [])
  assert.equal(tool.kind, 'read')
})

test('tool-search: createSearchToolsTool 命中则激活（带 sessionId）并返回清单', async () => {
  const calls: Array<{ names: string[]; sessionId: string }> = []
  const tool = createSearchToolsTool({
    listRemote: () => ENTRIES,
    activate: (names, sessionId) => {
      calls.push({ names, sessionId })
      return names
    }
  })
  const res = await tool.execute({ query: '设备' }, CTX)
  assert.equal(res.ok, true)
  assert.match(res.output, /mcp__device_ops__restart_device/)
  assert.deepEqual(calls, [{ names: ['mcp__device_ops__restart_device'], sessionId: CTX.sessionId }])
})

test('tool-search: createSearchToolsTool 未命中不激活，提示换关键词', async () => {
  let activated = false
  const tool = createSearchToolsTool({
    listRemote: () => ENTRIES,
    activate: () => {
      activated = true
      return []
    }
  })
  const res = await tool.execute({ query: '不存在的关键词xyz' }, CTX)
  assert.equal(res.ok, true)
  assert.match(res.output, /未找到/)
  assert.equal(activated, false)
})

test('tool-search: createSearchToolsTool limit 透传并夹紧，activate 抛错折叠为 ok:false', async () => {
  const many: ToolSearchEntry[] = Array.from({ length: 30 }, (_, i) => ({
    name: `mcp__srv__tool_${String(i).padStart(2, '0')}`,
    description: '通用工具'
  }))
  let lastCount = -1
  const tool = createSearchToolsTool({
    listRemote: () => many,
    activate: (names) => {
      lastCount = names.length
      return names
    }
  })
  await tool.execute({ query: 'tool', limit: 999 }, CTX)
  assert.equal(lastCount, SEARCH_TOOLS_MAX_LIMIT)

  const broken = createSearchToolsTool({
    listRemote: () => many,
    activate: () => {
      throw new Error('写盘失败')
    }
  })
  const res = await broken.execute({ query: 'tool' }, CTX)
  assert.equal(res.ok, false)
  assert.match(res.output, /工具搜索失败: 写盘失败/)
})
