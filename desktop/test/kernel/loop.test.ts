import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  assembleToolCalls,
  buildSystemPrompt,
  parseSseChoiceDelta,
  runAgentTurn,
  toWireMessages
} from '../../electron/main/kernel/loop'
import type { LoopDeps } from '../../electron/main/kernel/loop'
import type { AgentEvent, ToolDefinition } from '../../electron/main/kernel/types'
import type { Registry } from '../../electron/main/kernel/registry'
import type { PermissionGateway } from '../../electron/main/kernel/permissions'
import { buildToolNameMaps } from '../../electron/main/kernel/tool-names'

test('loop: parseSseChoiceDelta 返回 choices[0].delta（容忍 data: 前缀整行或裸 payload）', () => {
  const line = 'data: ' + JSON.stringify({ choices: [{ delta: { content: 'he' }, finish_reason: null }] })
  assert.deepEqual(parseSseChoiceDelta(line), { content: 'he' })
  assert.deepEqual(parseSseChoiceDelta(JSON.stringify({ choices: [{ delta: { content: 'he' } }] })), { content: 'he' })
})

test('loop: parseSseChoiceDelta 解析真实流式 tool_calls 增量片（id/name 仅首片）', () => {
  const frame = 'data: ' + JSON.stringify({
    choices: [{
      delta: { tool_calls: [{ index: 0, id: 'call_1', type: 'function', function: { name: 'file.read', arguments: '{"path' } }] },
      finish_reason: null
    }]
  })
  assert.deepEqual(parseSseChoiceDelta(frame), {
    tool_calls: [{ index: 0, id: 'call_1', type: 'function', function: { name: 'file.read', arguments: '{"path' } }]
  })
})

test('loop: parseSseChoiceDelta [DONE] 返回 null（固定语义：结束标记由流读取方判定）', () => {
  assert.equal(parseSseChoiceDelta('[DONE]'), null)
  assert.equal(parseSseChoiceDelta('data: [DONE]'), null)
})

test('loop: parseSseChoiceDelta 非法 JSON / 无 choices / 空输入返回 null', () => {
  assert.equal(parseSseChoiceDelta('not-json{{'), null)
  assert.equal(parseSseChoiceDelta('{"object":"chat.completion.chunk"}'), null)
  assert.equal(parseSseChoiceDelta('{"choices":[]}'), null)
  assert.equal(parseSseChoiceDelta(''), null)
})

test('loop: assembleToolCalls 乱序增量按 index 聚合，arguments 分片按到达顺序拼接', () => {
  const deltas = [
    { index: 1, id: 'call_def', function: { name: 'web.search', arguments: '{"q"' } },
    { index: 0, id: 'call_abc', function: { name: 'file.read', arguments: '{"path":"a' } },
    { index: 1, function: { arguments: ':"hi"}' } },
    { index: 0, function: { arguments: '.txt"}' } }
  ]
  assert.deepEqual(assembleToolCalls(deltas), [
    { id: 'call_abc', name: 'file.read', arguments: '{"path":"a.txt"}' },
    { id: 'call_def', name: 'web.search', arguments: '{"q":"hi"}' }
  ])
})

test('loop: assembleToolCalls 缺失 id/name 的条目以空串兜底', () => {
  assert.deepEqual(assembleToolCalls([{ index: 0, function: { arguments: '{}' } }]), [
    { id: '', name: '', arguments: '{}' }
  ])
})

test('loop: assembleToolCalls 空输入返回空数组', () => {
  assert.deepEqual(assembleToolCalls([]), [])
})

test('loop: buildSystemPrompt addendum 为空返回 base', () => {
  assert.equal(buildSystemPrompt('base', ''), 'base')
})

test('loop: buildSystemPrompt 非空 addendum 以空行拼接', () => {
  assert.equal(buildSystemPrompt('base', '## 可用技能'), 'base\n\n## 可用技能')
})

test('loop: toWireMessages assistant 带 toolCalls 时 content 置 null 并导出 tool_calls', () => {
  assert.deepEqual(
    toWireMessages([{ role: 'assistant', content: '', toolCalls: [{ id: 'call_1', name: 'f', arguments: '{"a":1}' }] }]),
    [{
      role: 'assistant',
      content: null,
      tool_calls: [{ id: 'call_1', type: 'function', function: { name: 'f', arguments: '{"a":1}' } }]
    }]
  )
})

test('loop: toWireMessages tool 消息映射 tool_call_id 且不带 name，system/user 原样', () => {
  assert.deepEqual(
    toWireMessages([
      { role: 'system', content: 'sys' },
      { role: 'user', content: 'hi' },
      { role: 'tool', content: 'out', toolCallId: 'call_1', name: 'f' }
    ]),
    [
      { role: 'system', content: 'sys' },
      { role: 'user', content: 'hi' },
      { role: 'tool', tool_call_id: 'call_1', content: 'out' }
    ]
  )
})

test('loop: toWireMessages 空输入返回空数组，assistant 无 toolCalls 保留字符串 content', () => {
  assert.deepEqual(toWireMessages([]), [])
  assert.deepEqual(toWireMessages([{ role: 'assistant', content: 'done' }]), [{ role: 'assistant', content: 'done' }])
})

test('loop: toWireMessages 缺省映射也 sanitize 兜底（不传映射时不发出非法名）', () => {
  assert.deepEqual(
    toWireMessages([{
      role: 'assistant',
      content: '',
      toolCalls: [{ id: 'c1', name: 'file.read', arguments: '{}' }]
    }]),
    [{
      role: 'assistant',
      content: null,
      tool_calls: [{ id: 'c1', type: 'function', function: { name: 'file_read', arguments: '{}' } }]
    }]
  )
})

test('loop: toWireMessages 带映射时历史 tool_calls 与 tool 消息名改写为 provider 合法名', () => {
  const toProvider = new Map([['file_read', 'file_read']])
  const wire = toWireMessages([
    { role: 'assistant', content: '', toolCalls: [{ id: 'call_1', name: 'file.read', arguments: '{"path":"a.txt"}' }] },
    { role: 'tool', content: 'hi', toolCallId: 'call_1', name: 'file.read' }
  ], toProvider)
  assert.deepEqual(wire, [
    {
      role: 'assistant',
      content: null,
      tool_calls: [{
        id: 'call_1',
        type: 'function',
        function: { name: 'file_read', arguments: '{"path":"a.txt"}' }
      }]
    },
    { role: 'tool', tool_call_id: 'call_1', content: 'hi', name: 'file_read' }
  ])
})

test('loop: toWireMessages 带映射时已合法的本地名保持既有 wire 形状（tool 消息不附 name）', () => {
  const toProvider = new Map([['file_read', 'file_read']])
  assert.deepEqual(
    toWireMessages([{ role: 'tool', content: 'out', toolCallId: 'c1', name: 'file_read' }], toProvider),
    [{ role: 'tool', tool_call_id: 'c1', content: 'out' }]
  )
  assert.deepEqual(
    toWireMessages([{ role: 'assistant', content: '', toolCalls: [{ id: 'c1', name: 'file_read', arguments: '{}' }] }], toProvider),
    [{
      role: 'assistant',
      content: null,
      tool_calls: [{ id: 'c1', type: 'function', function: { name: 'file_read', arguments: '{}' } }]
    }]
  )
})

// —— runAgentTurn 容错行为（离线桩 fetch 驱动完整循环） ——

/** 构造固定 SSE 帧序列的 Response，并捕获发往网关的 messages */
function stubSseFetch(frames: unknown[], sent: { messages: Array<Record<string, unknown>> }): typeof fetch {
  const encoder = new TextEncoder()
  return (async (_url: unknown, init?: RequestInit) => {
    sent.messages = (JSON.parse(String(init?.body)) as { messages: Array<Record<string, unknown>> }).messages
    return new Response(
      new ReadableStream<Uint8Array>({
        start(controller) {
          for (const frame of frames) controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`))
          controller.close()
        }
      }),
      { status: 200 }
    )
  }) as typeof fetch
}

/** 离线驱动 runAgentTurn 的最小 deps（registry / permissions 以桩替换） */
function loopDeps(overrides: Partial<LoopDeps>): LoopDeps {
  return {
    apiKey: 'test-key',
    baseUrl: 'http://127.0.0.1:3100',
    registry: { toOpenAiTools: () => [], get: () => undefined } as unknown as Registry,
    permissions: { ask: async () => true } as unknown as PermissionGateway,
    systemPrompt: 'sys',
    persist: () => {},
    ...overrides
  }
}

test('loop: runAgentTurn resolveSkill 抛异常按未命中处理，用户消息原样发送且 turn 正常完成', async () => {
  const sent: { messages: Array<Record<string, unknown>> } = { messages: [] }
  const originalFetch = globalThis.fetch
  globalThis.fetch = stubSseFetch([
    { choices: [{ delta: { content: '好的' }, finish_reason: null }] },
    { choices: [{ delta: {}, finish_reason: 'stop' }] }
  ], sent)
  const events: AgentEvent[] = []
  const persisted: Array<Record<string, unknown>> = []
  try {
    await runAgentTurn(loopDeps({
      resolveSkill: () => {
        throw new Error('技能不存在: notexist')
      },
      persist: (msg) => persisted.push(msg)
    }), {
      sessionId: 's1',
      workspace: 'w',
      model: 'm',
      history: [],
      userMessage: '/notexist 你好',
      emit: (e) => {
        events.push(e)
      }
    })
  } finally {
    globalThis.fetch = originalFetch
  }
  // 技能未命中：无技能 system 注入，user 消息原样发往网关并落盘
  assert.deepEqual(sent.messages, [
    { role: 'system', content: 'sys' },
    { role: 'user', content: '/notexist 你好' }
  ])
  assert.deepEqual(persisted, [
    { role: 'user', content: '/notexist 你好' },
    { role: 'assistant', content: '好的' }
  ])
  // turn 正常完成：token + done
  assert.deepEqual(events, [{ type: 'token', text: '好的' }, { type: 'done' }])
})

test('loop: runAgentTurn emit 抛异常不炸循环，后续 token/done 事件仍发出', async () => {
  const sent: { messages: Array<Record<string, unknown>> } = { messages: [] }
  const originalFetch = globalThis.fetch
  globalThis.fetch = stubSseFetch([
    { choices: [{ delta: { content: 'a' }, finish_reason: null }] },
    { choices: [{ delta: { content: 'b' }, finish_reason: null }] },
    { choices: [{ delta: {}, finish_reason: 'stop' }] }
  ], sent)
  const events: AgentEvent[] = []
  const persisted: Array<Record<string, unknown>> = []
  let emitCalls = 0
  try {
    await runAgentTurn(loopDeps({
      persist: (msg) => persisted.push(msg)
    }), {
      sessionId: 's1',
      workspace: 'w',
      model: 'm',
      history: [],
      userMessage: 'hi',
      emit: (e) => {
        emitCalls++
        if (emitCalls === 1) throw new Error('渲染进程已销毁')
        events.push(e)
      }
    })
  } finally {
    globalThis.fetch = originalFetch
  }
  // 首个 token 的 emit 抛异常被吞掉，后续 token 与 done 仍正常发出
  assert.deepEqual(events, [{ type: 'token', text: 'b' }, { type: 'done' }])
  // 循环完整走完：最终 assistant 消息（两个 token 拼接）已落盘
  assert.deepEqual(persisted, [
    { role: 'user', content: 'hi' },
    { role: 'assistant', content: 'ab' }
  ])
})

test('loop: runAgentTurn skipPersistUserMessage 复用已落盘用户消息, 只发请求不重复追加', async () => {
  const sent: { messages: Array<Record<string, unknown>> } = { messages: [] }
  const originalFetch = globalThis.fetch
  globalThis.fetch = stubSseFetch([
    { choices: [{ delta: { content: '重答' }, finish_reason: null }] },
    { choices: [{ delta: {}, finish_reason: 'stop' }] }
  ], sent)
  const persisted: Array<Record<string, unknown>> = []
  const events: AgentEvent[] = []
  try {
    await runAgentTurn(loopDeps({ persist: (msg) => persisted.push(msg) }), {
      sessionId: 's1',
      workspace: 'w',
      model: 'm',
      // 重新生成/编辑重发: history 为截断后的历史(不含目标用户消息), 目标消息单独作为 userMessage
      history: [{ role: 'assistant', content: '早前回答' }],
      userMessage: '接着问',
      skipPersistUserMessage: true,
      emit: (e) => {
        events.push(e)
      }
    })
  } finally {
    globalThis.fetch = originalFetch
  }
  // 用户消息仍进入请求上下文
  assert.deepEqual(sent.messages, [
    { role: 'system', content: 'sys' },
    { role: 'assistant', content: '早前回答' },
    { role: 'user', content: '接着问' }
  ])
  // 不再重复落盘用户消息, 只落最终回答
  assert.deepEqual(persisted, [{ role: 'assistant', content: '重答' }])
  assert.deepEqual(events, [{ type: 'token', text: '重答' }, { type: 'done' }])
})

test('loop: 技能命中仅在请求体展开(注入正文+剩余文本), 落盘保留原始 /skill 文本', async () => {
  const sent: { messages: Array<Record<string, unknown>> } = { messages: [] }
  const originalFetch = globalThis.fetch
  globalThis.fetch = stubSseFetch([
    { choices: [{ delta: { content: '好的' }, finish_reason: null }] },
    { choices: [{ delta: {}, finish_reason: 'stop' }] }
  ], sent)
  const persisted: Array<Record<string, unknown>> = []
  try {
    await runAgentTurn(loopDeps({
      resolveSkill: (name) => (name === 'demo' ? '技能正文' : null),
      persist: (msg) => persisted.push(msg)
    }), {
      sessionId: 's1',
      workspace: 'w',
      model: 'm',
      history: [],
      userMessage: '/demo 帮我写周报',
      emit: () => {}
    })
  } finally {
    globalThis.fetch = originalFetch
  }
  // 请求体: 注入技能 system 消息 + 剩余文本; UI/存储: 原始 /skill 文本
  assert.deepEqual(sent.messages, [
    { role: 'system', content: 'sys' },
    { role: 'system', content: '【技能：demo】\n技能正文' },
    { role: 'user', content: '帮我写周报' }
  ])
  assert.deepEqual(persisted, [
    { role: 'user', content: '/demo 帮我写周报' },
    { role: 'assistant', content: '好的' }
  ])
})

test('loop: 历史里的 /skill 消息在请求组装时按同一规则改写(重跑可复现)', async () => {
  const sent: { messages: Array<Record<string, unknown>> } = { messages: [] }
  const originalFetch = globalThis.fetch
  globalThis.fetch = stubSseFetch([
    { choices: [{ delta: { content: '继续答' }, finish_reason: null }] },
    { choices: [{ delta: {}, finish_reason: 'stop' }] }
  ], sent)
  try {
    await runAgentTurn(loopDeps({
      resolveSkill: (name) => (name === 'demo' ? '技能正文' : null)
    }), {
      sessionId: 's1',
      workspace: 'w',
      model: 'm',
      // 历史来自存储: 保留的是原始 /skill 文本
      history: [
        { role: 'user', content: '/demo 早前的请求' },
        { role: 'assistant', content: '早前回答' }
      ],
      userMessage: '继续',
      emit: () => {}
    })
  } finally {
    globalThis.fetch = originalFetch
  }
  assert.deepEqual(sent.messages, [
    { role: 'system', content: 'sys' },
    { role: 'system', content: '【技能：demo】\n技能正文' },
    { role: 'user', content: '早前的请求' },
    { role: 'assistant', content: '早前回答' },
    { role: 'user', content: '继续' }
  ])
})

test('loop: 工具执行中被中止则不 emit/不落盘结果, 半截工具轮整体不落盘', async () => {
  const executed: string[] = []
  const controller = new AbortController()
  const registry = registryOf([
    {
      name: 'slow_tool',
      description: '慢工具',
      kind: 'read',
      parameters: { type: 'object', properties: {} },
      execute: async () => {
        executed.push('slow_tool')
        controller.abort()
        return { ok: true, output: '慢工具完成' }
      }
    },
    toolDef('after_tool', executed)
  ])
  const bodies: Array<Record<string, unknown>> = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = stubSseRounds([
    [
      { choices: [{ delta: { tool_calls: [{ index: 0, id: 'c1', function: { name: 'slow_tool', arguments: '{}' } }] }, finish_reason: null }] },
      { choices: [{ delta: { tool_calls: [{ index: 1, id: 'c2', function: { name: 'after_tool', arguments: '{}' } }] }, finish_reason: null }] },
      { choices: [{ delta: {}, finish_reason: 'tool_calls' }] }
    ]
  ], bodies)
  const events: AgentEvent[] = []
  const persisted: Array<Record<string, unknown>> = []
  try {
    await runAgentTurn(loopDeps({ registry, persist: (msg) => persisted.push(msg) }), {
      sessionId: 's1',
      workspace: 'w',
      model: 'm',
      history: [],
      userMessage: '跑两个工具',
      signal: controller.signal,
      emit: (e) => {
        events.push(e)
      }
    })
  } finally {
    globalThis.fetch = originalFetch
  }
  // 第二个工具不执行; 首个工具的结果因执行期间被中止而丢弃(不 emit/不落盘)
  assert.deepEqual(executed, ['slow_tool'])
  assert.deepEqual(events.map((e) => e.type), ['tool_call', 'done'])
  // 用户消息已落盘, 但半截工具轮(assistant.tool_calls + tool 结果)整体不落盘
  assert.deepEqual(persisted.map((m) => m.role), ['user'])
  // 中止后不再进入下一轮请求
  assert.equal(bodies.length, 1)
})

// —— 工具名收敛（provider 合法字符）行为 ——

const LEGAL_TOOL_NAME = /^[a-zA-Z0-9_-]+$/

/** 最小可注册工具定义：execute 记录收到的本地名 */
function toolDef(name: string, executed: string[]): ToolDefinition {
  return {
    name,
    description: `${name} 描述`,
    kind: 'read',
    parameters: { type: 'object', properties: {} },
    execute: async () => {
      executed.push(name)
      return { ok: true, output: `by ${name}` }
    }
  }
}

/** 由工具定义构造 registry 桩：toOpenAiTools 输出原始本地名 */
function registryOf(tools: ToolDefinition[]): Registry {
  return {
    toOpenAiTools: () => tools.map(t => ({
      type: 'function' as const,
      function: { name: t.name, description: t.description, parameters: t.parameters }
    })),
    get: (name: string) => tools.find(t => t.name === name)
  } as unknown as Registry
}

/** 按轮次回放 SSE 帧并记录每轮完整请求体的 fetch 桩 */
function stubSseRounds(rounds: unknown[][], bodies: Array<Record<string, unknown>>): typeof fetch {
  const encoder = new TextEncoder()
  let round = 0
  return (async (_url: unknown, init?: RequestInit) => {
    bodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>)
    const frames = rounds[Math.min(round, rounds.length - 1)]
    round++
    return new Response(
      new ReadableStream<Uint8Array>({
        start(controller) {
          for (const frame of frames) controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`))
          controller.close()
        }
      }),
      { status: 200 }
    )
  }) as typeof fetch
}

test('loop: runAgentTurn 发出的 tools[].function.name 全为合法字符（含点工具被映射）', async () => {
  const executed: string[] = []
  const registry = registryOf([toolDef('file.read', executed), toolDef('mcp__设备运维__列出表', executed)])
  const bodies: Array<Record<string, unknown>> = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = stubSseRounds([[{ choices: [{ delta: {}, finish_reason: 'stop' }] }]], bodies)
  try {
    await runAgentTurn(loopDeps({ registry }), {
      sessionId: 's1',
      workspace: 'w',
      model: 'm',
      history: [],
      userMessage: 'hi',
      emit: () => {}
    })
  } finally {
    globalThis.fetch = originalFetch
  }
  const tools = bodies[0].tools as Array<{ function: { name: string } }>
  assert.equal(tools.length, 2)
  for (const t of tools) assert.match(t.function.name, LEGAL_TOOL_NAME)
  // 点号名被 sanitize，中文名不含中文
  const mapped = tools.map(t => t.function.name)
  assert.ok(mapped.includes('file_read'), mapped.join(','))
  assert.ok(mapped.every(n => !/[\u4e00-\u9fff]/.test(n)), mapped.join(','))
  assert.equal(new Set(mapped).size, mapped.length, 'provider 名互不重复')
})

test('loop: runAgentTurn provider 名解析回本地名，执行/emit/persist 用本地名，下一轮 wire 用 provider 名', async () => {
  const executed: string[] = []
  const dot = toolDef('file.read', executed)
  const registry = registryOf([dot])
  const bodies: Array<Record<string, unknown>> = []
  const events: AgentEvent[] = []
  const persisted: Array<Record<string, unknown>> = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = stubSseRounds([
    [
      { choices: [{ delta: { tool_calls: [{ index: 0, id: 'call_1', function: { name: 'file_read', arguments: '{}' } }] }, finish_reason: null }] },
      { choices: [{ delta: {}, finish_reason: 'tool_calls' }] }
    ],
    [
      { choices: [{ delta: { content: 'done' }, finish_reason: null }] },
      { choices: [{ delta: {}, finish_reason: 'stop' }] }
    ]
  ], bodies)
  try {
    await runAgentTurn(loopDeps({ registry, persist: msg => persisted.push(msg) }), {
      sessionId: 's1',
      workspace: 'w',
      model: 'm',
      history: [],
      userMessage: 'hi',
      emit: e => {
        events.push(e)
      }
    })
  } finally {
    globalThis.fetch = originalFetch
  }
  // provider 返回 file_read → 用本地名 file.read 解析并执行
  assert.deepEqual(executed, ['file.read'])
  // 事件流与落盘均使用本地名（存储/UI 保持本地名）
  assert.deepEqual(events.filter(e => e.type === 'tool_call'), [{ type: 'tool_call', name: 'file.read', args: '{}' }])
  assert.deepEqual(events.filter(e => e.type === 'tool_result'), [
    { type: 'tool_result', name: 'file.read', output: 'by file.read', ok: true }
  ])
  const assistant = persisted[1] as { role: string; toolCalls?: Array<{ name: string }> }
  assert.equal(assistant.role, 'assistant')
  assert.equal(assistant.toolCalls?.[0].name, 'file.read')
  const toolMsg = persisted[2] as { role: string; name?: string }
  assert.equal(toolMsg.role, 'tool')
  assert.equal(toolMsg.name, 'file.read')
  // 下一轮请求：历史 assistant tool_calls 与 tool 消息均改写为 provider 名
  const wireMessages = bodies[1].messages as Array<Record<string, unknown>>
  const wireAssistant = wireMessages.find(m => m.role === 'assistant') as {
    tool_calls: Array<{ function: { name: string } }>
  }
  assert.equal(wireAssistant.tool_calls[0].function.name, 'file_read')
  const wireTool = wireMessages.find(m => m.role === 'tool') as { name?: string }
  assert.equal(wireTool.name, 'file_read')
  for (const body of bodies) {
    for (const t of body.tools as Array<{ function: { name: string } }>) {
      assert.match(t.function.name, LEGAL_TOOL_NAME)
    }
  }
})

test('loop: runAgentTurn 中文 MCP 名在 wire 合法，回调经映射解析回原工具', async () => {
  const localName = 'mcp__设备运维__列出表'
  const executed: string[] = []
  const mcp = toolDef(localName, executed)
  const registry = registryOf([mcp])
  const { toProvider } = buildToolNameMaps([localName])
  const wireName = toProvider.get(localName)!
  const bodies: Array<Record<string, unknown>> = []
  const events: AgentEvent[] = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = stubSseRounds([
    [
      { choices: [{ delta: { tool_calls: [{ index: 0, id: 'call_mcp_1', function: { name: wireName, arguments: '{}' } }] }, finish_reason: null }] },
      { choices: [{ delta: {}, finish_reason: 'tool_calls' }] }
    ],
    [
      { choices: [{ delta: { content: 'ok' }, finish_reason: null }] },
      { choices: [{ delta: {}, finish_reason: 'stop' }] }
    ]
  ], bodies)
  try {
    await runAgentTurn(loopDeps({ registry }), {
      sessionId: 's1',
      workspace: 'w',
      model: 'm',
      history: [],
      userMessage: 'hi',
      emit: e => {
        events.push(e)
      }
    })
  } finally {
    globalThis.fetch = originalFetch
  }
  const sentTools = bodies[0].tools as Array<{ function: { name: string } }>
  assert.match(sentTools[0].function.name, LEGAL_TOOL_NAME)
  assert.equal(sentTools[0].function.name, wireName)
  assert.deepEqual(executed, [localName])
  assert.ok(events.some(e => e.type === 'tool_call' && e.name === localName))
  assert.ok(events.some(e => e.type === 'tool_result' && e.name === localName))
  // 下一轮历史里的 tool 消息附映射后的合法名
  const wireTool = (bodies[1].messages as Array<Record<string, unknown>>).find(m => m.role === 'tool') as { name?: string }
  assert.equal(wireTool.name, wireName)
})

test('loop: runAgentTurn 端到端改写旧历史 file.read，provider 回传 file_read 时按本地名执行并落盘', async () => {
  const executed: string[] = []
  const registry = registryOf([toolDef('file_read', executed)])
  const bodies: Array<Record<string, unknown>> = []
  const events: AgentEvent[] = []
  const persisted: Array<Record<string, unknown>> = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = stubSseRounds([
    [
      { choices: [{ delta: { tool_calls: [{ index: 0, id: 'call_new', function: { name: 'file_read', arguments: '{"path":"a.txt"}' } }] }, finish_reason: null }] },
      { choices: [{ delta: {}, finish_reason: 'tool_calls' }] }
    ],
    [
      { choices: [{ delta: { content: 'done' }, finish_reason: null }] },
      { choices: [{ delta: {}, finish_reason: 'stop' }] }
    ]
  ], bodies)
  try {
    await runAgentTurn(loopDeps({ registry, persist: msg => persisted.push(msg) }), {
      sessionId: 's1',
      workspace: 'w',
      model: 'm',
      // 旧会话历史：assistant tool_calls 与 tool 消息都存着带点旧名 file.read
      history: [
        { role: 'assistant', content: '', toolCalls: [{ id: 'old_call_1', name: 'file.read', arguments: '{"path":"old.txt"}' }] },
        { role: 'tool', content: '旧内容', toolCallId: 'old_call_1', name: 'file.read' }
      ],
      userMessage: '继续',
      emit: e => {
        events.push(e)
      }
    })
  } finally {
    globalThis.fetch = originalFetch
  }
  // 第 1 轮 wire：旧历史被改写为 provider 合法名 file_read
  const firstMessages = bodies[0].messages as Array<Record<string, unknown>>
  const wireAssistant = firstMessages.find(m => m.role === 'assistant') as {
    tool_calls: Array<{ id: string; function: { name: string } }>
  }
  assert.equal(wireAssistant.tool_calls[0].id, 'old_call_1')
  assert.equal(wireAssistant.tool_calls[0].function.name, 'file_read')
  const wireOldTool = firstMessages.find(m => m.role === 'tool') as { name?: string; tool_call_id: string }
  assert.equal(wireOldTool.tool_call_id, 'old_call_1')
  assert.equal(wireOldTool.name, 'file_read')
  // provider 回传 file_read：以本地名执行并 emit/落盘
  assert.deepEqual(executed, ['file_read'])
  assert.deepEqual(events.filter(e => e.type === 'tool_call'), [
    { type: 'tool_call', name: 'file_read', args: '{"path":"a.txt"}' }
  ])
  assert.ok(events.some(e => e.type === 'tool_result' && e.name === 'file_read'))
  const newToolMsg = persisted.find(m => m.role === 'tool') as { name?: string }
  assert.equal(newToolMsg.name, 'file_read')
  const newAssistant = persisted.find(m => m.role === 'assistant') as { toolCalls?: Array<{ name: string }> }
  assert.equal(newAssistant.toolCalls?.[0].name, 'file_read')
  // 第 2 轮 wire：新落盘的 assistant tool_calls 名合法；新工具消息本地名已合法，wire 保持既有形状（不带 name）
  const secondMessages = bodies[1].messages as Array<Record<string, unknown>>
  const secondAssistant = secondMessages.find(m => m.role === 'assistant' && Array.isArray(m.tool_calls)) as {
    tool_calls: Array<{ function: { name: string } }>
  }
  assert.equal(secondAssistant.tool_calls[0].function.name, 'file_read')
  const secondTool = secondMessages.filter(m => m.role === 'tool').pop() as { name?: string; tool_call_id: string }
  assert.equal(secondTool.tool_call_id, 'call_new')
  assert.equal(secondTool.name, undefined)
  for (const t of bodies[1].tools as Array<{ function: { name: string } }>) {
    assert.match(t.function.name, LEGAL_TOOL_NAME)
  }
})

// —— 渐进披露（工具搜索）：每轮按 toolSearch 钩子重组工具列表 ——

/** 生成 n 个远程工具（mcp__srv<i>__tool<i>），execute 记录本地名 */
function remoteToolDefs(n: number, executed: string[]): ToolDefinition[] {
  return Array.from({ length: n }, (_, i) => toolDef(`mcp__srv${i}__tool${i}`, executed))
}

test('loop: 渐进披露每轮重组——41 个远程工具只发内置+search_tools，激活后下一轮带上新工具', async () => {
  const executed: string[] = []
  const active: string[] = []
  const searchTool: ToolDefinition = {
    name: 'search_tools',
    description: '搜索远程工具',
    kind: 'read',
    parameters: { type: 'object', properties: { query: { type: 'string' } }, required: ['query'] },
    execute: async () => {
      executed.push('search_tools')
      // 模拟 ipc 侧激活：命中 2 个已注册工具 + 1 个已失效（不在 registry，应被组装过滤）
      active.push('mcp__srv0__tool0', 'market:market_search', 'mcp__gone__x')
      return { ok: true, output: '已激活' }
    }
  }
  const registry = registryOf([
    toolDef('file_read', executed),
    searchTool,
    toolDef('market:market_search', executed),
    ...remoteToolDefs(41, executed)
  ])
  const bodies: Array<Record<string, unknown>> = []
  const events: AgentEvent[] = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = stubSseRounds([
    [
      { choices: [{ delta: { tool_calls: [{ index: 0, id: 'c1', function: { name: 'search_tools', arguments: '{"query":"终端"}' } }] }, finish_reason: null }] },
      { choices: [{ delta: {}, finish_reason: 'tool_calls' }] }
    ],
    [
      { choices: [{ delta: { tool_calls: [{ index: 0, id: 'c2', function: { name: 'market_market_search', arguments: '{"params":{}}' } }] }, finish_reason: null }] },
      { choices: [{ delta: {}, finish_reason: 'tool_calls' }] }
    ],
    [
      { choices: [{ delta: { content: 'done' }, finish_reason: null }] },
      { choices: [{ delta: {}, finish_reason: 'stop' }] }
    ]
  ], bodies)
  try {
    await runAgentTurn(
      loopDeps({
        registry,
        toolSearch: { progressive: () => true, activeNames: () => [...active] }
      }),
      {
        sessionId: 's1',
        workspace: 'w',
        model: 'm',
        history: [],
        userMessage: 'hi',
        emit: e => {
          events.push(e)
        }
      }
    )
  } finally {
    globalThis.fetch = originalFetch
  }
  const namesOf = (i: number): string[] =>
    (bodies[i].tools as Array<{ function: { name: string } }>).map(t => t.function.name)
  // 第 1 轮：只发内置 file_read + search_tools，41 个远程工具不发
  assert.deepEqual(namesOf(0), ['file_read', 'search_tools'])
  // 激活的 2 个在 registry 中存在，第 2 轮带上；mcp__gone__x 未注册被过滤
  assert.deepEqual(namesOf(1).sort(), ['file_read', 'market_market_search', 'mcp__srv0__tool0', 'search_tools'])
  // provider 名映射覆盖激活后的工具：market_market_search 解析回本地名执行
  assert.deepEqual(executed, ['search_tools', 'market:market_search'])
  assert.ok(events.some(e => e.type === 'tool_call' && e.name === 'market:market_search'))
  // 第 3 轮仍只发内置 + 已激活的 2 个（会话内粘住）
  assert.deepEqual(namesOf(2).sort(), ['file_read', 'market_market_search', 'mcp__srv0__tool0', 'search_tools'])
  for (const body of bodies) {
    for (const t of body.tools as Array<{ function: { name: string } }>) {
      assert.match(t.function.name, LEGAL_TOOL_NAME)
    }
  }
})

test('loop: 未启用渐进(off/未注入)时每轮全量发送注册表工具', async () => {
  const executed: string[] = []
  const registry = registryOf([toolDef('file_read', executed), ...remoteToolDefs(41, executed)])
  const bodies: Array<Record<string, unknown>> = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = stubSseRounds([[{ choices: [{ delta: {}, finish_reason: 'stop' }] }]], bodies)
  try {
    // 未注入 toolSearch：旧行为
    await runAgentTurn(loopDeps({ registry }), {
      sessionId: 's1',
      workspace: 'w',
      model: 'm',
      history: [],
      userMessage: 'hi',
      emit: () => {}
    })
    // 显式 progressive=false：同样全量
    await runAgentTurn(
      loopDeps({ registry, toolSearch: { progressive: () => false, activeNames: () => ['mcp__srv0__tool0'] } }),
      {
        sessionId: 's1',
        workspace: 'w',
        model: 'm',
        history: [],
        userMessage: 'hi',
        emit: () => {}
      }
    )
  } finally {
    globalThis.fetch = originalFetch
  }
  assert.equal((bodies[0].tools as unknown[]).length, 42)
  assert.equal((bodies[1].tools as unknown[]).length, 42)
})

test('loop: 渐进模式(always, 远程为 0)仍只发内置+search_tools', async () => {
  const executed: string[] = []
  const registry = registryOf([toolDef('file_read', executed)])
  const bodies: Array<Record<string, unknown>> = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = stubSseRounds([[{ choices: [{ delta: {}, finish_reason: 'stop' }] }]], bodies)
  try {
    await runAgentTurn(
      loopDeps({ registry, toolSearch: { progressive: () => true, activeNames: () => [] } }),
      {
        sessionId: 's1',
        workspace: 'w',
        model: 'm',
        history: [],
        userMessage: 'hi',
        emit: () => {}
      }
    )
  } finally {
    globalThis.fetch = originalFetch
  }
  assert.deepEqual((bodies[0].tools as Array<{ function: { name: string } }>).map(t => t.function.name), ['file_read'])
})
