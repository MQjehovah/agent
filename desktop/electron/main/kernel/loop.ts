import type { PermissionGateway } from './permissions'
import type { Registry } from './registry'
import type { StoredMessage } from './session'
import type { AgentEvent, ChatMessage, ToolCall, ToolResult } from './types'
import { buildToolNameMaps, toProviderToolName } from './tool-names'
import { selectRoundTools } from './tool-search'
import { buildWireContent } from './image-wire'

/**
 * agent 工具调用循环：经 router 网关的 OpenAI 兼容 SSE 流驱动多轮对话，
 * 模型发起 tool_calls 时按注册表执行工具并把结果回填，直至最终作答或轮次用尽。
 * 工具列表每轮重算：连接器很多时按 ToolSearchHooks 走渐进披露（内置 + search_tools + 已激活远程工具）。
 * SSE 解析与 tool_calls 拼装为纯函数便于离线单测；完整网络循环由真机冒烟覆盖。
 */

/** 流式 tool_calls 增量片段（OpenAI wire 形状；id/name 仅首片携带） */
export interface ToolCallDelta {
  index: number
  id?: string
  function?: { name?: string; arguments?: string }
}

/** choices[0].delta 的宽松形状 */
export interface SseDelta {
  content?: string
  /** 推理模型(deepseek 等)的思维链增量 */
  reasoning_content?: string
  reasoning?: string
  tool_calls?: ToolCallDelta[]
}

/** 单帧解析结果：delta + finish_reason（loop 内部消费） */
interface SseFrame {
  delta: SseDelta | null
  finishReason: string | null
}

/**
 * 解析单条 SSE data 帧为 choices[0].delta。
 * 输入容忍整行（含 `data:` 前缀）或已剥离前缀的 payload；
 * [DONE] 固定返回 null（结束标记由流读取方判定）；无 choices / 非法 JSON / 空输入返回 null。
 */
export function parseSseChoiceDelta(frame: string): SseDelta | null {
  return parseSseFrame(frame)?.delta ?? null
}

/** 整帧解析：剥离 `data:` 前缀后取 JSON；[DONE] 与非法帧均返回 null */
function parseSseFrame(frame: string): SseFrame | null {
  const payload = frame.trim().replace(/^data:\s*/, '')
  if (!payload || payload === '[DONE]') return null
  try {
    const parsed: unknown = JSON.parse(payload)
    if (typeof parsed !== 'object' || parsed === null) return null
    const choices = (parsed as { choices?: unknown }).choices
    if (!Array.isArray(choices) || choices.length === 0) return null
    const choice = choices[0] as { delta?: unknown; finish_reason?: unknown }
    const delta = typeof choice.delta === 'object' && choice.delta !== null ? (choice.delta as SseDelta) : null
    const finishReason = typeof choice.finish_reason === 'string' ? choice.finish_reason : null
    if (delta === null && finishReason === null) return null
    return { delta, finishReason }
  } catch {
    return null
  }
}

/** 把乱序的 tool_calls 增量片段按 index 聚合为完整 ToolCall 列表（按 index 升序，缺省 id/name 以空串兜底） */
export function assembleToolCalls(deltas: ToolCallDelta[]): ToolCall[] {
  const acc = new Map<number, { id: string; name: string; parts: string[] }>()
  for (const d of deltas) {
    let entry = acc.get(d.index)
    if (!entry) {
      entry = { id: '', name: '', parts: [] }
      acc.set(d.index, entry)
    }
    if (d.id) entry.id = d.id
    if (d.function?.name) entry.name = d.function.name
    if (typeof d.function?.arguments === 'string') entry.parts.push(d.function.arguments)
  }
  return [...acc.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([, e]) => ({ id: e.id, name: e.name, arguments: e.parts.join('') }))
}

/** 组合系统提示词：addendum 非空时以空行拼接 */
export function buildSystemPrompt(base: string, addendum: string): string {
  return addendum ? `${base}\n\n${addendum}` : base
}

/**
 * 内核消息转 OpenAI wire 格式：assistant 带 toolCalls 时 content 置 null，tool 消息映射 tool_call_id。
 * 工具名恒经 sanitize：优先查 toProvider（当前注册名 → provider 名），查不到（含未传映射、旧历史名）
 * 走 toProviderToolName 兜底，因此任何情况下都不会发出非法名（旧会话的 file.read 也会变成 file_read，
 * 超长名截断到 64）。tool 消息仅在名字确实被改写时附带 name，已是合法名的正常路径保持既有 wire 形状不变。
 */
export function toWireMessages(
  messages: ChatMessage[],
  toProvider?: Map<string, string>
): Array<Record<string, unknown>> {
  const providerName = (name: string): string =>
    toProvider?.get(name) ?? toProviderToolName(name)
  return messages.map(msg => {
    if (msg.role === 'assistant' && msg.toolCalls?.length) {
      return {
        role: 'assistant',
        content: null,
        tool_calls: msg.toolCalls.map(tc => ({
          id: tc.id,
          type: 'function',
          function: { name: providerName(tc.name), arguments: tc.arguments }
        }))
      }
    }
    if (msg.role === 'tool') {
      const wire: Record<string, unknown> = {
        role: 'tool',
        tool_call_id: msg.toolCallId ?? '',
        content: msg.content
      }
      if (toProvider && msg.name) {
        const mapped = providerName(msg.name)
        if (mapped !== msg.name) wire.name = mapped
      }
      return wire
    }
    return { role: msg.role, content: msg.wireContent ?? msg.content }
  })
}

export interface LoopDeps {
  apiKey: string
  /** router 网关地址，如 http://127.0.0.1:3100 */
  baseUrl: string
  registry: Registry
  permissions: PermissionGateway
  /** 已由调用方拼好的系统提示词（基础 + 技能清单） */
  systemPrompt: string
  /** LLM 请求总轮数上限（含最终作答轮），默认 100 */
  maxRounds?: number
  /** 每条新增消息实时落盘（调用方注入 session store） */
  persist: (msg: Omit<StoredMessage, 'ts'>) => void
  /**
   * 技能正文解析（注入 skill loader 的 getSkillBody，保持 loop 可离线测试）。
   * 允许抛异常（如 getSkillBody 未找到技能时 throw）：loop 统一按「技能未命中」处理，用户消息原样发送
   */
  resolveSkill?: (name: string) => string | null
  /**
   * 渐进披露（工具搜索）钩子：每轮组装工具前现调，search_tools 激活的远程工具当轮即进入下一轮请求；
   * 缺省不传时保持旧行为（每轮全量发送注册表工具）。
   */
  toolSearch?: ToolSearchHooks
}

/** 渐进披露钩子：progressive 决定本轮是否只发内置 + 已激活远程工具，activeNames 提供本会话激活集 */
export interface ToolSearchHooks {
  progressive(): boolean
  activeNames(): string[]
}

export interface RunTurnInput {
  sessionId: string
  workspace: string
  model: string
  /** buildContext 结果，不含 system */
  history: ChatMessage[]
  userMessage: string
  /**
   * true 时用户消息只进入请求上下文、不再落盘。
   * 重新生成/编辑重发场景: 目标用户消息已在存储里(截断点), 复用它避免重复追加。
   */
  skipPersistUserMessage?: boolean
  emit: (e: AgentEvent) => void
  signal?: AbortSignal
}

/** 技能触发语法：/技能名 剩余文本 */
const SKILL_RE = /^\/([\w-]+)\s*([\s\S]*)$/

/**
 * 请求组装时的技能改写：用户消息以 /技能名 开头且能解析到正文时，
 * 在请求体里注入技能 system 消息、并把该条用户消息替换为剩余文本。
 *
 * 存储与 UI 始终保留原始文本（含 `/skill ...`）：重跑（重新生成/编辑重发）可复现，
 * 历史里的技能消息也会在每次请求时按同一规则改写。
 * resolveSkill 允许抛异常（如 getSkillBody 未找到技能时 throw），统一按「技能未命中」原样发送。
 */
export function applySkillsToMessages(
  messages: ChatMessage[],
  resolveSkill?: (name: string) => string | null
): ChatMessage[] {
  if (!resolveSkill) return messages
  const out: ChatMessage[] = []
  for (const m of messages) {
    if (m.role !== 'user') {
      out.push(m)
      continue
    }
    const match = m.content.match(SKILL_RE)
    if (!match) {
      out.push(m)
      continue
    }
    let body: string | null = null
    try {
      body = resolveSkill(match[1]) ?? null
    } catch (err) {
      console.warn(`[kernel] resolveSkill('${match[1]}') 抛异常，按未命中处理: ${errMsg(err)}`)
    }
    if (body === null) {
      out.push(m)
      continue
    }
    out.push({ role: 'system', content: `【技能：${match[1]}】\n${body}` })
    out.push({ role: 'user', content: match[2] })
  }
  return out
}

/** 单轮流式 outcome：累积的 assistant 文本 / finish_reason / tool_calls 增量，或提前终止标记 */
interface RoundOutcome {
  content: string
  reasoning: string
  finishReason: string | null
  toolDeltas: ToolCallDelta[]
  /** 本轮 provider 名 → 本地工具名映射（执行侧解析工具用，请求前已构建） */
  toLocal: Map<string, string>
  aborted?: boolean
  error?: string
}

export async function runAgentTurn(deps: LoopDeps, input: RunTurnInput): Promise<void> {
  const maxRounds = deps.maxRounds ?? 100
  const messages: ChatMessage[] = [{ role: 'system', content: deps.systemPrompt }, ...input.history]

  // 入口处给 emit 包一层 safeEmit：UI 侧 emit 抛异常只告警，不得炸循环或穿透 error 分支
  const safeEmit = (e: AgentEvent): void => {
    try {
      input.emit(e)
    } catch (err) {
      console.warn(`[kernel] emit 回调异常（${e.type}），已忽略: ${errMsg(err)}`)
    }
  }

  // 已中止：不发起任何请求，直接结束
  if (input.signal?.aborted) {
    safeEmit({ type: 'done' })
    return
  }

  try {
    // 技能改写发生在请求组装时（applySkillsToMessages），存储与 UI 保留原始用户文本：
    // 重新生成/编辑重发可直接复用原文，技能正文不会丢。
    if (!input.skipPersistUserMessage) deps.persist({ role: 'user', content: input.userMessage })
    // 多模态：消息里的图片附件路径内联成图片（仅本轮；历史仍存文本路径）
    const userWire = buildWireContent(input.userMessage, input.workspace)
    messages.push(
      userWire === input.userMessage
        ? { role: 'user', content: input.userMessage }
        : { role: 'user', content: input.userMessage, wireContent: userWire }
    )

    for (let round = 0; round < maxRounds; round++) {
      if (input.signal?.aborted) {
        safeEmit({ type: 'done' })
        return
      }

      const outcome = await streamRound(deps, input, messages, safeEmit)
      if (outcome.aborted) {
        safeEmit({ type: 'done' })
        return
      }
      if (outcome.error) {
        safeEmit({ type: 'error', message: outcome.error })
        return
      }

      const toolCalls = outcome.toolDeltas.length ? assembleToolCalls(outcome.toolDeltas) : []

      // 模型请求工具（finish_reason=tool_calls 或已收到增量）：逐个执行，全部成功才落盘。
      // 中止语义：工具执行前/后检查 signal，已中止则不再执行/emit/persist 剩余工具，
      // 且**半截工具轮整体不落盘**（避免 assistant.tool_calls 与 tool 结果不配对）。
      if (outcome.finishReason === 'tool_calls' || toolCalls.length) {
        const toLocal = outcome.toLocal
        const localCalls = toolCalls.map(tc => ({ ...tc, name: toLocal.get(tc.name) ?? tc.name }))
        const results: Array<{ call: ToolCall; localName: string; output: string; ok: boolean }> = []
        let aborted = false
        for (const call of toolCalls) {
          if (input.signal?.aborted) {
            aborted = true
            break
          }
          const localName = toLocal.get(call.name) ?? call.name
          safeEmit({ type: 'tool_call', name: localName, args: call.arguments })
          const result = await execToolCall(deps, input, call, toLocal)
          if (input.signal?.aborted) {
            // 执行期间被中止：结果不 emit/不落盘，与既有中止语义一致
            aborted = true
            break
          }
          safeEmit({ type: 'tool_result', name: localName, output: result.output, ok: result.ok })
          results.push({ call, localName, output: result.output, ok: result.ok })
        }
        if (aborted) {
          safeEmit({ type: 'done' })
          return
        }
        // 存储与 UI 一律写本地名（下一轮请求再经 toWireMessages 映射回 provider 名）
        deps.persist({ role: 'assistant', content: outcome.content, toolCalls: localCalls })
        messages.push({ role: 'assistant', content: outcome.content, toolCalls: localCalls })
        for (const r of results) {
          deps.persist({ role: 'tool', content: r.output, toolCallId: r.call.id, name: r.localName })
          messages.push({ role: 'tool', content: r.output, toolCallId: r.call.id, name: r.localName })
        }
        continue
      }

      // 最终作答：persist 最终 assistant 消息后结束
      deps.persist({ role: 'assistant', content: outcome.content })
      safeEmit({ type: 'done' })
      return
    }
    safeEmit({ type: 'error', message: `已达最大工具调用轮数（${maxRounds}）` })
  } catch (err) {
    // streamRound / 工具执行之外的兜底（如 persist 抛错），保证事件流不被静默吞掉
    safeEmit({ type: 'error', message: `agent 循环异常: ${errMsg(err)}` })
  }
}

/** 发起一轮流式请求并消费 SSE：token 增量实时 emit，tool_calls 增量累积待拼装 */
async function streamRound(deps: LoopDeps, input: RunTurnInput, messages: ChatMessage[], emit: (e: AgentEvent) => void): Promise<RoundOutcome> {
  // 请求边界：把注册表本地工具名（file.read / 中文 MCP 名 / market:<name> 等）收敛为 provider 合法名；
  // 旧历史里的非法名由 toWireMessages 按同一规则 sanitize 兜底。映射只作用于 wire，存储与 UI 保持本地名。
  // 渐进披露：每轮按 toolSearch 钩子现算——只发内置工具 + search_tools + 本会话已激活的远程工具，
  // search_tools 激活后下一轮重组即生效；缺省钩子时保持全量（现状）。
  const openAiTools = selectRoundTools(
    deps.registry.toOpenAiTools(),
    deps.toolSearch?.progressive() ?? false,
    deps.toolSearch?.activeNames() ?? []
  )
  const { toProvider, toLocal } = buildToolNameMaps(openAiTools.map(t => t.function.name))
  const tools = openAiTools.map(t => {
    const mapped = toProvider.get(t.function.name) ?? t.function.name
    return mapped === t.function.name ? t : { ...t, function: { ...t.function, name: mapped } }
  })
  let response: Response
  try {
    response = await fetch(`${deps.baseUrl}/v1/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${deps.apiKey}`
      },
      body: JSON.stringify({
        model: input.model,
        // 技能改写只作用于请求体（存储保留原文）: /skill 消息在此注入技能正文并替换为剩余文本
        messages: toWireMessages(applySkillsToMessages(messages, deps.resolveSkill), toProvider),
        tools,
        stream: true
      }),
      signal: input.signal
    })
  } catch (err) {
    if (isAbortError(err)) return { content: '', reasoning: '', finishReason: null, toolDeltas: [], toLocal, aborted: true }
    return { content: '', reasoning: '', finishReason: null, toolDeltas: [], toLocal, error: `请求失败: ${errMsg(err)}` }
  }

  if (!response.ok) {
    const body = await safeReadBody(response)
    if (response.status === 401) {
      return { content: '', reasoning: '', finishReason: null, toolDeltas: [], toLocal, error: 'apikey 无效或已过期，请重新 SSO 登录' }
    }
    return {
      content: '',
      reasoning: '',
      finishReason: null,
      toolDeltas: [],
      toLocal,
      error: `请求失败 (HTTP ${response.status}): ${truncate(body, 500)}`
    }
  }
  if (!response.body) {
    return { content: '', reasoning: '', finishReason: null, toolDeltas: [], toLocal, error: '响应体为空' }
  }

  const outcome: RoundOutcome = { content: '', reasoning: '', finishReason: null, toolDeltas: [], toLocal }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let nl: number
      while ((nl = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, nl).replace(/\r$/, '')
        buffer = buffer.slice(nl + 1)
        consumeLine(line, outcome, emit)
      }
    }
    // 刷出解码器残余（流末尾无换行的最后一行）
    const rest = buffer + decoder.decode()
    if (rest) consumeLine(rest, outcome, emit)
  } catch (err) {
    if (isAbortError(err)) return { ...outcome, aborted: true }
    return { ...outcome, error: `读取流失败: ${errMsg(err)}` }
  }
  return outcome
}

/** 消费单行 SSE：非 data 行忽略；token 增量实时 emit，tool_calls 增量原样累积 */
function consumeLine(line: string, outcome: RoundOutcome, emit: (e: AgentEvent) => void): void {
  if (!line.startsWith('data:')) return
  const frame = parseSseFrame(line)
  if (!frame) return
  if (frame.finishReason) outcome.finishReason = frame.finishReason
  const delta = frame.delta
  if (!delta) return
  const reasoning = delta.reasoning_content ?? delta.reasoning
  if (typeof reasoning === 'string' && reasoning) {
    emit({ type: 'reasoning', text: reasoning })
    outcome.reasoning += reasoning
  }
  if (typeof delta.content === 'string' && delta.content) {
    emit({ type: 'token', text: delta.content })
    outcome.content += delta.content
  }
  if (Array.isArray(delta.tool_calls)) {
    for (const tc of delta.tool_calls) {
      if (tc && typeof tc === 'object' && typeof (tc as ToolCallDelta).index === 'number') {
        outcome.toolDeltas.push(tc as ToolCallDelta)
      }
    }
  }
}

/** 执行单个工具调用：未知工具/用户拒绝/非法参数/执行异常均折叠为 ok:false 的工具结果；provider 名先解析回本地名 */
async function execToolCall(
  deps: LoopDeps,
  input: RunTurnInput,
  call: ToolCall,
  toLocal: Map<string, string>
): Promise<ToolResult> {
  const localName = toLocal.get(call.name) ?? call.name
  const tool = deps.registry.get(localName)
  if (!tool) return { ok: false, output: `未知工具: ${localName}` }
  if (tool.kind === 'write' && deps.permissions.needsAsk(localName, tool.kind)) {
    const allowed = await deps.permissions.ask(input.sessionId, localName, truncate(call.arguments, 200))
    if (!allowed) return { ok: false, output: '用户拒绝执行该工具' }
  }
  let args: Record<string, unknown>
  try {
    // 无参工具允许空 arguments
    args = JSON.parse(call.arguments || '{}') as Record<string, unknown>
  } catch {
    return { ok: false, output: '工具参数不是合法 JSON' }
  }
  try {
    return await tool.execute(args, { workspace: input.workspace, sessionId: input.sessionId })
  } catch (err) {
    return { ok: false, output: `工具执行异常: ${errMsg(err)}` }
  }
}

function isAbortError(err: unknown): boolean {
  return err instanceof Error && err.name === 'AbortError'
}

function errMsg(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + '…' : s
}

async function safeReadBody(response: Response): Promise<string> {
  try {
    return await response.text()
  } catch {
    return ''
  }
}
