import { test } from 'node:test'
import assert from 'node:assert/strict'
import { appendFileSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createSessionStore, parseModelIds, resolveRunModel } from '../../electron/main/kernel/session'

const dataDir = mkdtempSync(join(tmpdir(), 'stest-'))
const store = createSessionStore(dataDir)

test('session: create/list/append/getMessages roundtrip keeps tool fields', () => {
  const before = store.listSessions().length
  const s = store.createSession({ model: 'glm-4', workspace: 'ws', systemPrompt: '你是助手' })
  assert.ok(s.id.startsWith('local-'), 'id 应带 local- 前缀')
  assert.equal(s.mode, 'local')
  assert.equal(s.model, 'glm-4')
  assert.equal(s.workspace, 'ws')
  assert.equal(s.systemPrompt, '你是助手')
  assert.ok(store.listSessions().length === before + 1)
  assert.ok(store.listSessions().some(x => x.id === s.id))

  // assistant 消息带 toolCalls，显式 ts 应被尊重
  // 故意保留旧版点号名 file.read：存储原样落盘，历史兼容与 wire 改写由 loop/tool-names 测试覆盖
  const a = store.appendMessage(s.id, {
    role: 'assistant', content: '',
    toolCalls: [{ id: 'call_1', name: 'file.read', arguments: '{"path":"a.txt"}' }],
    ts: 12345
  })
  assert.equal(a.ts, 12345)

  // tool 消息带 toolCallId 与 name
  store.appendMessage(s.id, { role: 'tool', content: 'hello', toolCallId: 'call_1', name: 'file.read' })

  const msgs = store.getMessages(s.id)
  assert.equal(msgs.length, 2)
  assert.equal(msgs[0].ts, 12345)
  assert.deepEqual(msgs[0].toolCalls, [{ id: 'call_1', name: 'file.read', arguments: '{"path":"a.txt"}' }])
  assert.equal(msgs[1].toolCallId, 'call_1')
  assert.equal(msgs[1].name, 'file.read')
})

test('session: default title uses local datetime', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  const expect = `本地会话 ${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
  assert.equal(s.title, expect)
})

test('session: delete clears list and getSession returns null', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  store.appendMessage(s.id, { role: 'user', content: 'hi' })
  store.deleteSession(s.id)
  assert.equal(store.getSession(s.id), null)
  assert.equal(store.listSessions().find(x => x.id === s.id), undefined)
  assert.equal(store.getMessages(s.id).length, 0)
})

test('session: buildContext drops older middle messages but keeps latest two', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  // 5 条消息，每条 content 长 10，内容可区分
  for (let i = 1; i <= 5; i++) {
    store.appendMessage(s.id, { role: 'user', content: (`msg${i}`).padEnd(10, '.') })
  }
  // 预算 35：最新两条(20)始终保留，倒数第三条(10)恰好可进，更早的超限丢弃
  const ctx = store.buildContext(s.id, 35)
  assert.equal(ctx.length, 3)
  assert.deepEqual(ctx.map(m => m.content), ['msg3......', 'msg4......', 'msg5......'])
  // 返回 ChatMessage：不携带 ts 等存储元数据
  assert.ok(ctx.every(m => !('ts' in m)))
  // toolCalls 等可选字段透传
  const s2 = store.createSession({ model: 'm', workspace: 'ws' })
  store.appendMessage(s2.id, {
    role: 'assistant', content: '',
    toolCalls: [{ id: 'c9', name: 'glob', arguments: '{}' }]
  })
  const ctx2 = store.buildContext(s2.id, 100)
  assert.deepEqual(ctx2[0].toolCalls, [{ id: 'c9', name: 'glob', arguments: '{}' }])
})

test('session: buildContext keeps latest two even when all exceed budget', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  store.appendMessage(s.id, { role: 'user', content: 'a'.repeat(10) })
  store.appendMessage(s.id, { role: 'assistant', content: 'b'.repeat(10) })
  store.appendMessage(s.id, { role: 'user', content: 'c'.repeat(10) })
  // 预算 5 连一条都装不下，仍须保留最近两条保证最近一轮完整
  const ctx = store.buildContext(s.id, 5)
  assert.equal(ctx.length, 2)
  assert.deepEqual(ctx.map(m => m.content), ['b'.repeat(10), 'c'.repeat(10)])
})

test('session: buildContext returns all when within budget', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  for (let i = 1; i <= 4; i++) store.appendMessage(s.id, { role: 'user', content: `n${i}` })
  const ctx = store.buildContext(s.id, 1000)
  assert.equal(ctx.length, 4)
  assert.deepEqual(ctx.map(m => m.content), ['n1', 'n2', 'n3', 'n4'])
})

test('session: buildContext keeps exactly two messages at tight budget', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  for (let i = 1; i <= 4; i++) {
    store.appendMessage(s.id, { role: 'user', content: 'x'.repeat(10) })
  }
  // 预算 25：第三条起累计 30 已超限 → 只保留最近两条
  const ctx = store.buildContext(s.id, 25)
  assert.equal(ctx.length, 2)
})

test('session: corrupted jsonl lines are skipped', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  store.appendMessage(s.id, { role: 'user', content: 'a' })
  const file = join(dataDir, 'localagent', 'sessions', `${s.id}.messages.jsonl`)
  appendFileSync(file, '{broken json\n')
  store.appendMessage(s.id, { role: 'user', content: 'b' })
  const msgs = store.getMessages(s.id)
  assert.deepEqual(msgs.map(m => m.content), ['a', 'b'])
})

test('session: multiple sessions do not interfere', () => {
  const a = store.createSession({ model: 'm1', workspace: 'ws1' })
  const b = store.createSession({ model: 'm2', workspace: 'ws2' })
  assert.notEqual(a.id, b.id)
  store.appendMessage(a.id, { role: 'user', content: 'from-a' })
  store.appendMessage(b.id, { role: 'user', content: 'from-b' })
  assert.deepEqual(store.getMessages(a.id).map(m => m.content), ['from-a'])
  assert.deepEqual(store.getMessages(b.id).map(m => m.content), ['from-b'])
  assert.equal(store.getSession(a.id)?.model, 'm1')
  assert.equal(store.getSession(b.id)?.model, 'm2')
  // 删除 a 不影响 b
  store.deleteSession(a.id)
  assert.equal(store.getSession(a.id), null)
  assert.equal(store.getSession(b.id)?.model, 'm2')
})

test('session: appendMessage repairs crash-truncated last line', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  const file = join(dataDir, 'localagent', 'sessions', `${s.id}.messages.jsonl`)
  // 模拟崩溃残留：半截 JSON 行且无换行符
  appendFileSync(file, '{"role":"user","content":"half', 'utf-8')
  const stored = store.appendMessage(s.id, { role: 'user', content: 'after-crash' })
  // 半截行被跳过，新消息完整保留
  const msgs = store.getMessages(s.id)
  assert.deepEqual(msgs.map(m => m.content), ['after-crash'])
  // 新消息独立成行（未被拼进损坏行）：半截行 / 新消息行 / 末尾换行
  assert.deepEqual(readFileSync(file, 'utf-8').split('\n'), [
    '{"role":"user","content":"half',
    JSON.stringify(stored),
    ''
  ])
})

test('session: buildContext budget counts toolCalls size', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  const toolCalls = [{ id: 'c1', name: 'file.read', arguments: '{"pad":"' + 'x'.repeat(50) + '"}' }]
  store.appendMessage(s.id, { role: 'user', content: 'old' })
  store.appendMessage(s.id, { role: 'assistant', content: '', toolCalls })
  store.appendMessage(s.id, { role: 'user', content: 'new' })
  // 预算 = 前两条体积再减 1（'new' 3 + '' 0 + toolCalls 序列化体积），'old'(3) 塞不下
  const toolCallsSize = JSON.stringify(toolCalls).length
  const budget = toolCallsSize + 5
  const ctx = store.buildContext(s.id, budget)
  assert.deepEqual(ctx.map(m => m.content), ['', 'new'])
  // 对照：若预算忽略 toolCalls（前两条 used 仅 6），'old'(3) 本可塞进该预算 → 本测试能有效区分两种实现
  assert.ok(budget >= 9)
})

test('session: invalid id throws instead of path escape', () => {
  assert.throws(() => store.getSession('../../evil'), /非法会话 id/)
  assert.throws(() => store.appendMessage('../../evil', { role: 'user', content: 'x' }), /非法会话 id/)
  assert.throws(() => store.getMessages('../../evil'), /非法会话 id/)
  assert.throws(() => store.deleteSession('../../evil'), /非法会话 id/)
  assert.throws(() => store.buildContext('../../evil', 100), /非法会话 id/)
})

test('session: listSessions skips meta with missing or ill-typed fields', () => {
  const dir = join(dataDir, 'localagent', 'sessions')
  // 缺 createdAt 的元数据 → 跳过
  writeFileSync(
    join(dir, 'local-deadbeef.meta.json'),
    JSON.stringify({ id: 'local-deadbeef', mode: 'local', title: 't', model: 'm', workspace: 'w' }),
    'utf-8'
  )
  // 非 JSON 的元数据 → 跳过
  writeFileSync(join(dir, 'local-cafebabe.meta.json'), 'not json', 'utf-8')
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  const ids = store.listSessions().map(x => x.id)
  assert.ok(!ids.includes('local-deadbeef'))
  assert.ok(!ids.includes('local-cafebabe'))
  assert.ok(ids.includes(s.id))
})

test('session: deleteSession on unknown id is a no-op', () => {
  assert.doesNotThrow(() => store.deleteSession('local-00000000'))
})

test('session: createSession 携带 persona 落盘并可读出', () => {
  const s = store.createSession({
    model: 'glm-4',
    workspace: 'ws',
    persona: { name: '销售小助手', prompt: '你是资深销售专家' }
  })
  const meta = store.getSession(s.id)
  assert.deepEqual(meta?.persona, { name: '销售小助手', prompt: '你是资深销售专家' })
  assert.ok(store.listSessions().some((x) => x.id === s.id && x.persona?.name === '销售小助手'))
  // 不带 persona 的会话不受影响（向后兼容）
  const plain = store.createSession({ model: 'glm-4', workspace: 'ws' })
  assert.equal(store.getSession(plain.id)?.persona, undefined)
})

test('session: teardown removes temp dir', () => {
  rmSync(dataDir, { recursive: true, force: true })
})


test('session: setModel 写回元数据并持久化, 非法输入返回 null', () => {
  const s = store.createSession({ model: 'deepseek-flash', workspace: 'ws' })
  const updated = store.setModel(s.id, '  deepseek-v4-pro  ')
  assert.ok(updated, '应返回更新后的 meta')
  assert.equal(updated!.model, 'deepseek-v4-pro')
  assert.equal(store.getSession(s.id)!.model, 'deepseek-v4-pro')
  assert.ok(updated!.updatedAt >= s.updatedAt)

  // 空模型/不存在会话/非法 id
  assert.equal(store.setModel(s.id, '   '), null)
  assert.equal(store.setModel('local-00000000', 'm'), null)
  assert.throws(() => store.setModel('../escape', 'm'))
})

test('session: resolveRunModel 会话级优先, 缺省回退默认值', () => {
  assert.equal(resolveRunModel('glm-5.3', 'deepseek-flash'), 'glm-5.3')
  assert.equal(resolveRunModel('', 'deepseek-flash'), 'deepseek-flash')
  assert.equal(resolveRunModel(undefined, ' deepseek-flash '), 'deepseek-flash')
  assert.equal(resolveRunModel('  ', '  '), '')
})

test('session: parseModelIds 去重保序并过滤空值', () => {
  assert.deepEqual(parseModelIds({ data: [{ id: 'a' }, { id: 'b' }, { id: 'a' }, { id: '  ' }, {}] }), ['a', 'b'])
  assert.deepEqual(parseModelIds({ data: [] }), [])
  assert.deepEqual(parseModelIds(null), [])
  assert.deepEqual(parseModelIds({ data: 'nope' }), [])
})

test('session: buildContext 过滤孤儿 tool 消息(纵深防御), 存储原样保留', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  store.appendMessage(s.id, { role: 'user', content: '问' })
  // 孤儿: toolCallId 没有对应的 assistant.tool_calls(中止/崩溃残留)
  store.appendMessage(s.id, { role: 'tool', content: '孤儿结果', toolCallId: 'ghost', name: 'x' })
  store.appendMessage(s.id, {
    role: 'assistant',
    content: '',
    toolCalls: [{ id: 'c1', name: 'read_file', arguments: '{}' }]
  })
  store.appendMessage(s.id, { role: 'tool', content: '正常结果', toolCallId: 'c1', name: 'read_file' })

  const ctx = store.buildContext(s.id, 100_000)
  assert.deepEqual(ctx.map((m) => m.role), ['user', 'assistant', 'tool'])
  assert.deepEqual(ctx.map((m) => m.content), ['问', '', '正常结果'])
  // 仅过滤请求上下文: 存储与 UI 快照仍能看到原始 4 条(便于排查)
  assert.equal(store.getMessages(s.id).length, 4)
})

test('session: estimateContext 与 buildContext 同口径(超预算时 used/kept 截断, total 仍为全量)', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  for (let i = 1; i <= 5; i++) {
    store.appendMessage(s.id, { role: 'user', content: `msg${i}`.padEnd(10, '.') })
  }
  // 预算 35: 与 buildContext 用例一致 → 保留 msg3..msg5
  const est = store.estimateContext(s.id, 35)
  assert.equal(est.kept, 3)
  assert.equal(est.used, 30)
  assert.equal(est.total, 50)
  // 与 buildContext 的实际发送体积一致
  const ctx = store.buildContext(s.id, 35)
  assert.equal(est.used, ctx.reduce((sum, m) => sum + m.content.length, 0))
  assert.equal(est.kept, ctx.length)
  // 预算充足时 used=total, 全部保留
  const full = store.estimateContext(s.id, 1000)
  assert.equal(full.kept, 5)
  assert.equal(full.used, 50)
  assert.equal(full.total, 50)
})

test('session: estimateContext 空会话归零, 且忽略孤儿 tool 体积', () => {
  const empty = store.createSession({ model: 'm', workspace: 'ws' })
  assert.deepEqual(store.estimateContext(empty.id, 1000), { used: 0, kept: 0, total: 0 })

  const s = store.createSession({ model: 'm', workspace: 'ws' })
  store.appendMessage(s.id, { role: 'user', content: '问' })
  store.appendMessage(s.id, { role: 'tool', content: '孤'.repeat(500), toolCallId: 'ghost', name: 'x' })
  const est = store.estimateContext(s.id, 1000)
  assert.equal(est.total, 1)
  assert.equal(est.used, 1)
  assert.equal(est.kept, 1)
})

test('session: estimateContext 非法 id 抛错, toolCalls 体积计入', () => {
  assert.throws(() => store.estimateContext('../../evil', 100), /非法会话 id/)
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  const toolCalls = [{ id: 'c1', name: 'read_file', arguments: '{"pad":"' + 'x'.repeat(50) + '"}' }]
  store.appendMessage(s.id, { role: 'user', content: 'old' })
  store.appendMessage(s.id, { role: 'assistant', content: '', toolCalls })
  store.appendMessage(s.id, { role: 'user', content: 'new' })
  const est = store.estimateContext(s.id, 5)
  // 预算 5 连一条都装不下: 最近两条仍保留, used 计入 toolCalls 序列化体积
  assert.equal(est.kept, 2)
  assert.equal(est.used, 3 + JSON.stringify(toolCalls).length)
  assert.equal(est.total, 6 + JSON.stringify(toolCalls).length)
})

test('session: 预算恰切在 assistant.tool_calls 与 tool 之间时, 二次过滤孤儿 tool 并重算 used', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  const toolCalls = [{ id: 'c1', name: 'read_file', arguments: '{}' }]
  store.appendMessage(s.id, { role: 'assistant', content: '', toolCalls })
  store.appendMessage(s.id, { role: 'tool', content: '结果', toolCallId: 'c1', name: 'read_file' })
  store.appendMessage(s.id, { role: 'user', content: '新问题' })
  // 预算 = 最近两条('新问题' 3 + '结果' 2)恰好装下 → 回溯到 assistant 时超限被切,
  // 首条 kept 变成无前置声明的孤儿 tool(预算切断 mid-round)
  const budget = '新问题'.length + '结果'.length
  const ctx = store.buildContext(s.id, budget)
  assert.deepEqual(ctx.map((m) => m.role), ['user'])
  assert.deepEqual(ctx.map((m) => m.content), ['新问题'])
  // 估算与发送口径一致: 孤儿 tool 不计入 used
  const est = store.estimateContext(s.id, budget)
  assert.equal(est.kept, 1)
  assert.equal(est.used, '新问题'.length)
  assert.equal(est.total, '结果'.length + '新问题'.length + JSON.stringify(toolCalls).length)
  // 对照: 预算足够时 assistant+tool 完整保留, 不被误伤
  const full = store.buildContext(s.id, 1000)
  assert.deepEqual(full.map((m) => m.role), ['assistant', 'tool', 'user'])
})

// —— 渐进披露：会话激活集（session meta activeRemoteTools） ——

test('session: 激活集往返(trim/去重), 新会话为空, 跨 store 实例可读回', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  assert.deepEqual(store.getActiveRemoteTools(s.id), [])
  const first = store.addActiveRemoteTools(s.id, ['mcp__a__x', 'mcp__a__x', '  market:b  '])
  assert.deepEqual(first, ['mcp__a__x', 'market:b'])
  assert.deepEqual(store.getActiveRemoteTools(s.id), ['mcp__a__x', 'market:b'])
  assert.deepEqual(store.getSession(s.id)?.activeRemoteTools, ['mcp__a__x', 'market:b'])
  // 落盘后可被新 store 实例读回（meta JSON）
  const reopened = createSessionStore(dataDir)
  assert.deepEqual(reopened.getActiveRemoteTools(s.id), ['mcp__a__x', 'market:b'])
  // 追加合并与去重
  assert.deepEqual(store.addActiveRemoteTools(s.id, ['market:b', 'mcp__c__y']), [
    'mcp__a__x',
    'market:b',
    'mcp__c__y'
  ])
})

test('session: 激活集覆盖写, 空集移除字段', () => {
  const s = store.createSession({ model: 'm', workspace: 'ws' })
  assert.deepEqual(store.setActiveRemoteTools(s.id, ['mcp__a__x']), ['mcp__a__x'])
  assert.deepEqual(store.getActiveRemoteTools(s.id), ['mcp__a__x'])
  assert.deepEqual(store.setActiveRemoteTools(s.id, ['market:z', 'mcp__a__x']), ['market:z', 'mcp__a__x'])
  assert.deepEqual(store.getActiveRemoteTools(s.id), ['market:z', 'mcp__a__x'])
  // 空数组删除字段: meta 不再含 activeRemoteTools 键
  assert.deepEqual(store.setActiveRemoteTools(s.id, []), [])
  assert.equal(store.getSession(s.id)?.activeRemoteTools, undefined)
})

test('session: 激活集对缺失/类型错误/损坏 meta 容错(不抛, 按空处理)', () => {
  // 会话不存在 / 非法 id
  assert.deepEqual(store.getActiveRemoteTools('local-00000000'), [])
  assert.deepEqual(store.addActiveRemoteTools('local-00000000', ['mcp__a__x']), [])
  assert.deepEqual(store.setActiveRemoteTools('local-00000000', ['mcp__a__x']), [])
  assert.throws(() => store.getActiveRemoteTools('../escape'), /非法会话 id/)

  // 字段类型错误(字符串而非数组) → 按空处理; add 过滤非法元素后覆盖为合法数组
  const bad = store.createSession({ model: 'm', workspace: 'ws' })
  const badFile = join(dataDir, 'localagent', 'sessions', `${bad.id}.meta.json`)
  const meta = JSON.parse(readFileSync(badFile, 'utf-8')) as Record<string, unknown>
  meta.activeRemoteTools = 'not-an-array'
  writeFileSync(badFile, JSON.stringify(meta), 'utf-8')
  assert.deepEqual(store.getActiveRemoteTools(bad.id), [])
  assert.deepEqual(store.addActiveRemoteTools(bad.id, ['mcp__a__x', 42, null] as unknown as string[]), [
    'mcp__a__x'
  ])

  // meta 整体损坏(非 JSON) → 按空处理, 且 set/add 不覆盖原文件
  const broken = store.createSession({ model: 'm', workspace: 'ws' })
  const brokenFile = join(dataDir, 'localagent', 'sessions', `${broken.id}.meta.json`)
  writeFileSync(brokenFile, '{broken', 'utf-8')
  assert.deepEqual(store.getActiveRemoteTools(broken.id), [])
  assert.deepEqual(store.setActiveRemoteTools(broken.id, ['mcp__a__x']), [])
  assert.deepEqual(store.addActiveRemoteTools(broken.id, ['mcp__a__x']), [])
  assert.equal(readFileSync(brokenFile, 'utf-8'), '{broken')
})
