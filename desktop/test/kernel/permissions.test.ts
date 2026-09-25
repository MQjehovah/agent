import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createPermissions } from '../../electron/main/kernel/permissions'
import { builtinTools } from '../../electron/main/kernel/tools'

interface NotifyReq {
  requestId: string
  sessionId: string
  tool: string
  summary: string
}

/** 收集 notify 调用的桩 */
function stubNotify() {
  const calls: NotifyReq[] = []
  const notify = (req: NotifyReq) => { calls.push(req) }
  return { calls, notify }
}

test('permissions: ask 挂起并同步 notify 携带 requestId', async () => {
  const { calls, notify } = stubNotify()
  const gw = createPermissions(notify)
  const p = gw.ask('s1', 'write_file', '写入 a.txt')
  // notify 同步调用，requestId 已生成
  assert.equal(calls.length, 1)
  assert.equal(calls[0].sessionId, 's1')
  assert.equal(calls[0].tool, 'write_file')
  assert.equal(calls[0].summary, '写入 a.txt')
  assert.ok(calls[0].requestId.length > 0)
  // 未 respond 前保持未决：轮询确认未 resolve
  let settled = false
  void p.then(() => { settled = true })
  await new Promise((r) => setTimeout(r, 10))
  assert.equal(settled, false)
  gw.respond(calls[0].requestId, 'allow')
  assert.equal(await p, true)
})

test('permissions: cancel 将同会话全部 pending 以 false resolve 且不影响其他会话', async () => {
  const { calls, notify } = stubNotify()
  const gw = createPermissions(notify)
  const p1 = gw.ask('s1', 't1', 'a')
  const p2 = gw.ask('s1', 't2', 'b')
  const p3 = gw.ask('s2', 't1', 'c')
  // 无 pending 的会话 cancel 安全 no-op
  assert.doesNotThrow(() => gw.cancel('no-such-session'))
  gw.cancel('s1')
  assert.deepEqual(await Promise.all([p1, p2]), [false, false])
  // 其他会话的 pending 不受影响，仍可正常裁决
  let settled3 = false
  void p3.then(() => { settled3 = true })
  await new Promise((r) => setTimeout(r, 10))
  assert.equal(settled3, false)
  gw.respond(calls[2].requestId, 'allow')
  assert.equal(await p3, true)
})

test('permissions: cancel 后 respond 原 requestId 安全 no-op 不复活不误放行', async () => {
  const { calls, notify } = stubNotify()
  const gw = createPermissions(notify)
  const p = gw.ask('s1', 'write_file', 'a')
  gw.cancel('s1')
  assert.equal(await p, false)
  // 对已取消的 requestId respond：不抛错、不复活请求、不写入放行集
  assert.doesNotThrow(() => gw.respond(calls[0].requestId, 'allow_session'))
  const p2 = gw.ask('s1', 'write_file', 'b')
  assert.equal(calls.length, 2)
  gw.respond(calls[1].requestId, 'allow')
  assert.equal(await p2, true)
})

test('permissions: cancel(s) 默认保留会话放行，cancel(s, true) 清除后再次询问', async () => {
  const { calls, notify } = stubNotify()
  const gw = createPermissions(notify)
  const p1 = gw.ask('s1', 'run_cmd', 'a')
  gw.respond(calls[0].requestId, 'allow_session')
  assert.equal(await p1, true)
  // 默认 cancel（clearGrants 缺省 false）：放行保留，同工具 ask 直接 true 不 notify
  gw.cancel('s1')
  assert.equal(await gw.ask('s1', 'run_cmd', 'b'), true)
  assert.equal(calls.length, 1)
  // clearGrants = true：放行被清除，同工具 ask 需重新 notify 询问
  gw.cancel('s1', true)
  const p2 = gw.ask('s1', 'run_cmd', 'c')
  assert.equal(calls.length, 2)
  gw.respond(calls[1].requestId, 'allow')
  assert.equal(await p2, true)
})

test('permissions: deny 后同 session+tool 再次 ask 仍触发 notify 可再次裁决', async () => {
  const { calls, notify } = stubNotify()
  const gw = createPermissions(notify)
  const p1 = gw.ask('s1', 'write_file', 'a')
  assert.equal(calls.length, 1)
  gw.respond(calls[0].requestId, 'deny')
  assert.equal(await p1, false)
  // deny 不写入放行集：再次 ask 必须再次 notify，且拿到新 requestId 可独立裁决
  const p2 = gw.ask('s1', 'write_file', 'b')
  assert.equal(calls.length, 2)
  assert.notEqual(calls[1].requestId, calls[0].requestId)
  gw.respond(calls[1].requestId, 'allow')
  assert.equal(await p2, true)
})

test('permissions: allow → true，deny → false', async () => {
  const { calls, notify } = stubNotify()
  const gw = createPermissions(notify)
  const p1 = gw.ask('s1', 'write_file', 'a')
  const p2 = gw.ask('s1', 'write_file', 'b')
  gw.respond(calls[0].requestId, 'allow')
  gw.respond(calls[1].requestId, 'deny')
  assert.equal(await p1, true)
  assert.equal(await p2, false)
})

test('permissions: allow_session 后同 session+tool 的后续 ask 直接放行且不再 notify', async () => {
  const { calls, notify } = stubNotify()
  const gw = createPermissions(notify)
  const p = gw.ask('s1', 'run_cmd', 'ls')
  assert.equal(calls.length, 1)
  gw.respond(calls[0].requestId, 'allow_session')
  assert.equal(await p, true)
  // 后续 ask 不再 notify，直接 resolve true
  assert.equal(await gw.ask('s1', 'run_cmd', 'pwd'), true)
  assert.equal(calls.length, 1)
})

test('permissions: 不同 sessionId 的同名工具互不影响', async () => {
  const { calls, notify } = stubNotify()
  const gw = createPermissions(notify)
  const p1 = gw.ask('s1', 'run_cmd', 'a')
  gw.respond(calls[0].requestId, 'allow_session')
  assert.equal(await p1, true)
  // s2 同名工具仍需询问
  const p2 = gw.ask('s2', 'run_cmd', 'b')
  assert.equal(calls.length, 2)
  let settled = false
  void p2.then(() => { settled = true })
  await new Promise((r) => setTimeout(r, 10))
  assert.equal(settled, false)
  gw.respond(calls[1].requestId, 'deny')
  assert.equal(await p2, false)
})

test('permissions: respond 未知 requestId 安全 no-op', () => {
  const { notify } = stubNotify()
  const gw = createPermissions(notify)
  assert.doesNotThrow(() => gw.respond('nonexistent', 'allow'))
  assert.doesNotThrow(() => gw.respond('nonexistent', 'allow_session'))
})

test('permissions: 重复 respond 同一 requestId 只生效一次', async () => {
  const { calls, notify } = stubNotify()
  const gw = createPermissions(notify)
  const p = gw.ask('s1', 'write_file', 'a')
  gw.respond(calls[0].requestId, 'allow')
  // 第二次 respond 是 no-op，不允许把已删除的 pending 复活或误加放行集
  gw.respond(calls[0].requestId, 'deny')
  assert.equal(await p, true)
})

test('permissions: 并发乱序 respond 结果正确', async () => {
  const { calls, notify } = stubNotify()
  const gw = createPermissions(notify)
  const p1 = gw.ask('s1', 't1', 'a')
  const p2 = gw.ask('s1', 't2', 'b')
  const p3 = gw.ask('s2', 't1', 'c')
  gw.respond(calls[1].requestId, 'deny')
  gw.respond(calls[2].requestId, 'allow_session')
  gw.respond(calls[0].requestId, 'allow')
  assert.deepEqual(await Promise.all([p1, p2, p3]), [true, false, true])
})

test('permissions: notify 抛异常不影响 ask 挂起语义', async () => {
  const gw = createPermissions(() => { throw new Error('UI 崩了') })
  const p = gw.ask('s1', 'write_file', 'a')
  let settled = false
  void p.then(() => { settled = true }, () => { settled = true })
  await new Promise((r) => setTimeout(r, 10))
  assert.equal(settled, false)
  // notify 抛异常时 ask 不应同步抛出；此处拿不到 requestId，验证 no-op 后仍可正常走通其他请求
  const { calls, notify } = stubNotify()
  const gw2 = createPermissions((req) => {
    if (calls.length === 0) { calls.push(req); throw new Error('第一次抛') }
    notify(req)
  })
  const p2 = gw2.ask('s1', 'write_file', 'b')
  assert.equal(calls.length, 1)
  gw2.respond(calls[0].requestId, 'allow')
  assert.equal(await p2, true)
  void p
})

// —— 权限模式 × 工具名（needsAsk 按名 + 可选 kind 判定；内建 write 的工具级把关由调用方负责） ——

test('permissions: smart 模式询问 terminal 与 MCP 写工具，内建文件写/读不问', () => {
  const gw = createPermissions(() => {})
  gw.setMode('smart')
  assert.equal(gw.needsAsk('terminal', 'write'), true)
  // MCP 工具带外部副作用：smart 下应确认（含中文服务名）
  assert.equal(gw.needsAsk('mcp__x__write', 'write'), true)
  assert.equal(gw.needsAsk('mcp__设备运维__列出表', 'write'), true)
  // MCP 只读工具不问
  assert.equal(gw.needsAsk('mcp__x__read', 'read'), false)
  // 市场远程工具（market: 前缀）不在 smart 询问范围
  assert.equal(gw.needsAsk('market:weather', 'write'), false)
  // 回归锚点：内建工具里只有 terminal 属高危，其余（含 file_write/file_edit）smart 下都不问
  for (const t of builtinTools) {
    assert.equal(gw.needsAsk(t.name, t.kind), t.name === 'terminal', `${t.name} 在 smart 模式下的期望应为 ${t.name === 'terminal'}`)
  }
})

test('permissions: default 模式 needsAsk 恒 true，调用方再按 tool.kind === "write" 把关（既有语义）', () => {
  const gw = createPermissions(() => {})
  gw.setMode('default')
  for (const t of builtinTools) assert.equal(gw.needsAsk(t.name, t.kind), true, t.name)
  assert.equal(gw.needsAsk('mcp__x__write', 'write'), true)
  // 实际弹窗 = kind === 'write' && needsAsk：write 类确认、read 类放行（与 loop.execToolCall 的判定一致）
  const asks = (name: string): boolean => {
    const t = builtinTools.find(x => x.name === name)
    assert.ok(t, `缺少内建工具 ${name}`)
    return t!.kind === 'write' && gw.needsAsk(name, t!.kind)
  }
  for (const name of ['file_write', 'file_edit', 'terminal']) assert.equal(asks(name), true, name)
  for (const name of ['file_read', 'glob', 'grep']) assert.equal(asks(name), false, name)
})

test('permissions: auto 模式全部无需确认', () => {
  const gw = createPermissions(() => {})
  gw.setMode('auto')
  for (const t of builtinTools) assert.equal(gw.needsAsk(t.name, t.kind), false, t.name)
  assert.equal(gw.needsAsk('terminal', 'write'), false)
  assert.equal(gw.needsAsk('mcp__x__write', 'write'), false)
})

// —— 权限记忆：allow_always 跨会话/重启永久免问 ——

/** 内存桩：模拟 permission-memory 注入的 { has, add } */
function stubMemory(initial: string[] = []) {
  const tools = new Set(initial)
  const added: string[] = []
  return {
    tools,
    added,
    memory: {
      has: (tool: string) => tools.has(tool),
      add: (tool: string) => {
        added.push(tool)
        tools.add(tool)
      }
    }
  }
}

test('permissions: 已记住的工具 needsAsk 恒 false（优先于 default/smart 模式）', () => {
  const { memory } = stubMemory(['terminal', 'mcp__x__write'])
  const gw = createPermissions(() => {}, memory)
  gw.setMode('default')
  assert.equal(gw.needsAsk('terminal', 'write'), false)
  assert.equal(gw.needsAsk('mcp__x__write', 'write'), false)
  // 未记住的工具不受影响
  assert.equal(gw.needsAsk('file_write', 'write'), true)
  gw.setMode('smart')
  assert.equal(gw.needsAsk('terminal', 'write'), false)
  assert.equal(gw.needsAsk('mcp__x__write', 'write'), false)
  assert.equal(gw.needsAsk('mcp__x__read', 'read'), false)
})

test('permissions: 已记住的工具 ask 直接 true 且不 notify（双保险短路）', async () => {
  const { calls, notify } = stubNotify()
  const { memory } = stubMemory(['terminal'])
  const gw = createPermissions(notify, memory)
  assert.equal(await gw.ask('s1', 'terminal', 'ls'), true)
  assert.equal(calls.length, 0, '已记住工具不应弹窗')
})

test('permissions: respond(allow_always) 写入记忆并 resolve true，后续跨会话/重启免问', async () => {
  const { calls, notify } = stubNotify()
  const { added, memory } = stubMemory()
  const gw = createPermissions(notify, memory)
  const p = gw.ask('s1', 'terminal', 'rm -rf')
  assert.equal(calls.length, 1)
  gw.respond(calls[0].requestId, 'allow_always')
  assert.equal(await p, true)
  assert.deepEqual(added, ['terminal'], 'allow_always 应调用 memory.add')
  // 模拟重启：新网关注入同一记忆，同工具（任意会话）直接放行不 notify
  const gw2 = createPermissions(notify, memory)
  assert.equal(gw2.needsAsk('terminal', 'write'), false)
  assert.equal(await gw2.ask('s-other', 'terminal', 'pwd'), true)
  assert.equal(calls.length, 1, '记住后不应产生新请求')
})

test('permissions: allow_session 不写记忆（仅会话级），allow/deny 亦不写', async () => {
  const { calls, notify } = stubNotify()
  const { added, memory } = stubMemory()
  const gw = createPermissions(notify, memory)
  const p1 = gw.ask('s1', 'run_cmd', 'a')
  gw.respond(calls[0].requestId, 'allow_session')
  assert.equal(await p1, true)
  const p2 = gw.ask('s2', 'run_cmd', 'b')
  gw.respond(calls[1].requestId, 'allow')
  assert.equal(await p2, true)
  const p3 = gw.ask('s3', 'run_cmd', 'c')
  gw.respond(calls[2].requestId, 'deny')
  assert.equal(await p3, false)
  assert.deepEqual(added, [], '只有 allow_always 才落盘记忆')
})

test('permissions: 未知 requestId 的 allow_always 不写记忆', () => {
  const { memory, added } = stubMemory()
  const gw = createPermissions(() => {}, memory)
  assert.doesNotThrow(() => gw.respond('nonexistent', 'allow_always'))
  assert.deepEqual(added, [])
})

test('permissions: memory.add 抛异常不阻断本次放行（resolve true）', async () => {
  const { calls, notify } = stubNotify()
  const gw = createPermissions(notify, {
    has: () => false,
    add: () => { throw new Error('磁盘满了') }
  })
  const orig = console.warn
  console.warn = () => {}
  try {
    const p = gw.ask('s1', 'terminal', 'a')
    gw.respond(calls[0].requestId, 'allow_always')
    assert.equal(await p, true)
  } finally {
    console.warn = orig
  }
})
