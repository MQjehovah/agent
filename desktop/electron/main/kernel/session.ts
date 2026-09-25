import { randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync, appendFileSync } from 'node:fs'
import { join } from 'node:path'
import type { ChatMessage } from './types'

/**
 * 本地会话持久化与上下文窗口。
 * 每次操作直接读写文件（无内存缓存），进程内单线程使用，无需锁。
 * 布局：<dataDir>/localagent/sessions/<id>.meta.json + <id>.messages.jsonl（每行一条 JSON）。
 */

/** 本地会话元数据（落盘为 <id>.meta.json） */
export interface LocalSession {
  id: string
  mode: 'local'
  title: string
  model: string
  workspace: string
  systemPrompt?: string
  /** 可选的人设：来自本地 agents 目录的市场 agent 能力，chat 时 prompt 作为 systemPrompt 前缀 */
  persona?: { name: string; prompt: string }
  /**
   * 渐进披露(工具搜索)：本会话已激活的远程工具名，search_tools 命中后写入，会话内粘住；
   * 新会话缺省空数组，缺失/损坏一律按空处理（旧 meta 兼容）
   */
  activeRemoteTools?: string[]
  createdAt: number
  updatedAt: number
  /** 临时会话(仅本地): 应用启动/退出时自动删除, 不落历史 */
  ephemeral?: true
}

/** 带落盘时间戳的消息 */
export interface StoredMessage extends ChatMessage {
  ts: number
}

export interface SessionStore {
  listSessions(): LocalSession[]
  createSession(input: {
    title?: string
    model: string
    workspace: string
    systemPrompt?: string
    persona?: { name: string; prompt: string }
    /** 临时会话(仅本地): 退出/启动时清理, UI 有「临时」徽标 */
    ephemeral?: boolean
  }): LocalSession
  deleteSession(id: string): void
  /** 重命名本地会话(标题最长 60 字); 会话不存在返回 null */
  renameSession(id: string, title: string): LocalSession | null
  /** 切换会话使用的模型(按会话选择, 最长 64 字); 会话不存在/模型为空返回 null */
  setModel(id: string, model: string): LocalSession | null
  getSession(id: string): LocalSession | null
  appendMessage(id: string, msg: Omit<StoredMessage, 'ts'> & { ts?: number }): StoredMessage
  getMessages(id: string): StoredMessage[]
  buildContext(id: string, maxChars: number): ChatMessage[]
  /**
   * 上下文用量估算（与 buildContext 完全同口径，供输入区徽标）:
   * used=将被发送的字符数, kept=参与上下文的消息条数, total=过滤孤儿 tool 后全部字符数。
   */
  estimateContext(id: string, maxChars: number): { used: number; kept: number; total: number }
  /**
   * 原子重写全部消息(临时文件 + rename), 并刷新会话 updatedAt;
   * 用于重新生成/编辑重发/截断。缺省 ts 补当前时间。
   */
  rewriteMessages(id: string, messages: Array<Omit<StoredMessage, 'ts'> & { ts?: number }>): StoredMessage[]
  /** 截断到前 keepCount 条(keepCount 越界按边界处理, 损坏行不保留); 返回保留的消息 */
  truncateMessages(id: string, keepCount: number): StoredMessage[]
  /**
   * 读取会话已激活的远程工具(渐进披露)；meta 缺失/损坏/字段非法一律返回空数组(容错, 不抛)
   */
  getActiveRemoteTools(id: string): string[]
  /** 合并激活远程工具(trim/去重/过滤非法值)并持久化; 会话 meta 缺失/损坏时返回空数组且不写盘 */
  addActiveRemoteTools(id: string, names: string[]): string[]
  /** 覆盖激活集(连接器禁用/卸载时剔除失效工具用)；空数组会移除该字段，返回最新列表 */
  setActiveRemoteTools(id: string, names: string[]): string[]
}

/** 会话存储目录：<dataDir>/localagent/sessions */
function sessionsDir(dataDir: string): string {
  return join(dataDir, 'localagent', 'sessions')
}

function metaPath(dataDir: string, id: string): string {
  return join(sessionsDir(dataDir), `${id}.meta.json`)
}

function messagesPath(dataDir: string, id: string): string {
  return join(sessionsDir(dataDir), `${id}.messages.jsonl`)
}

/** 会话 id 形如 local-<8位十六进制>；id 直接拼文件路径，渲染进程回传的 id 必须先校验防路径逃逸 */
function assertValidId(id: string): void {
  if (!/^local-[0-9a-f]{8}$/.test(id)) throw new Error('非法会话 id')
}

/** 元数据形状校验：关键字段缺失或类型不对的条目视为损坏，跳过不抛 */
function isShapedMeta(v: unknown): v is LocalSession {
  if (typeof v !== 'object' || v === null) return false
  const m = v as Record<string, unknown>
  return (
    typeof m.id === 'string' &&
    typeof m.title === 'string' &&
    typeof m.model === 'string' &&
    typeof m.workspace === 'string' &&
    typeof m.createdAt === 'number'
  )
}

/** title 缺省：'本地会话 ' + 本地日期(YYYY-MM-DD HH:mm) */
function defaultTitle(now: number): string {
  const d = new Date(now)
  const p = (n: number) => String(n).padStart(2, '0')
  return `本地会话 ${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** StoredMessage 去掉 ts 等存储元数据，还原为 ChatMessage */
function toChatMessage(m: StoredMessage): ChatMessage {
  const out: ChatMessage = { role: m.role, content: m.content }
  if (m.toolCalls) out.toolCalls = m.toolCalls
  if (m.toolCallId) out.toolCallId = m.toolCallId
  if (m.name) out.name = m.name
  return out
}

/** 消息体积：content 之外 toolCalls 序列化后也可能很大，忽略会低估上下文预算 */
function messageSize(m: StoredMessage): number {
  return m.content.length + (m.toolCalls ? JSON.stringify(m.toolCalls).length : 0)
}

/** 激活集规范化：过滤非字符串/空串，trim 后按首次出现去重（损坏 meta 的容错边界） */
function normalizeToolNames(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  const out: string[] = []
  for (const item of value) {
    if (typeof item !== 'string') continue
    const name = item.trim()
    if (!name || out.includes(name)) continue
    out.push(name)
  }
  return out
}

/**
 * 上下文选取（buildContext 与 estimateContext 共用的唯一口径）：
 * 从最新往回收集，最近两条始终保留（保证最近一轮对话完整），更早的超限丢弃。
 * 截断后再做一次孤儿 tool 过滤：预算切断可能恰好落在 assistant.tool_calls 与 tool 之间，
 * 保留无前置声明的 tool 消息会让 provider 400（used 同步重算，保证徽标口径=实际发送）。
 */
export function selectContextMessages(
  msgs: readonly StoredMessage[],
  maxChars: number
): { kept: StoredMessage[]; used: number } {
  const kept: StoredMessage[] = []
  let used = 0
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (kept.length >= 2 && used + messageSize(msgs[i]) > maxChars) break
    used += messageSize(msgs[i])
    kept.push(msgs[i])
  }
  const ordered = kept.reverse()
  const filtered = dropOrphanToolMessages(ordered)
  if (filtered.length !== ordered.length) {
    used = filtered.reduce((sum, m) => sum + messageSize(m), 0)
  }
  return { kept: filtered, used }
}

/** 崩溃恢复：文件末尾若无换行（半截行），先补 \n 再追加，避免新消息拼进损坏行被一起丢掉 */
function ensureTrailingNewline(file: string): void {
  if (!existsSync(file)) return
  const buf = readFileSync(file)
  if (buf.length > 0 && buf[buf.length - 1] !== 0x0a) appendFileSync(file, '\n', 'utf-8')
}

export function createSessionStore(dataDir: string): SessionStore {
  const ensureDir = () => mkdirSync(sessionsDir(dataDir), { recursive: true })

  return {
    listSessions() {
      const dir = sessionsDir(dataDir)
      if (!existsSync(dir)) return []
      const out: LocalSession[] = []
      for (const name of readdirSync(dir)) {
        if (!name.endsWith('.meta.json')) continue
        try {
          const meta: unknown = JSON.parse(readFileSync(join(dir, name), 'utf-8'))
          // 形状不对（如缺 createdAt、字段类型不符）的条目跳过，不影响其余会话
          if (isShapedMeta(meta)) out.push(meta)
        } catch {
          // 损坏的元数据文件跳过，不影响其余会话
        }
      }
      return out.sort((a, b) => a.createdAt - b.createdAt)
    },

    createSession(input) {
      ensureDir()
      const now = Date.now()
      const session: LocalSession = {
        id: `local-${randomBytes(4).toString('hex')}`,
        mode: 'local',
        title: input.title ?? defaultTitle(now),
        model: input.model,
        workspace: input.workspace,
        systemPrompt: input.systemPrompt,
        createdAt: now,
        updatedAt: now
      }
      // persona 属可选增强：只在显式传入时写入，落盘/读出与旧版本 meta 兼容
      if (input.persona) session.persona = input.persona
      if (input.ephemeral) session.ephemeral = true
      writeFileSync(metaPath(dataDir, session.id), JSON.stringify(session, null, 2), 'utf-8')
      return session
    },

    deleteSession(id) {
      assertValidId(id)
      rmSync(metaPath(dataDir, id), { force: true })
      rmSync(messagesPath(dataDir, id), { force: true })
    },

    renameSession(id, title) {
      assertValidId(id)
      const file = metaPath(dataDir, id)
      if (!existsSync(file)) return null
      let meta: LocalSession
      try {
        meta = JSON.parse(readFileSync(file, 'utf-8')) as LocalSession
      } catch {
        return null
      }
      meta.title = String(title || '').trim().slice(0, 60) || meta.title
      meta.updatedAt = Date.now()
      writeFileSync(file, JSON.stringify(meta), 'utf-8')
      return meta
    },

    setModel(id, model) {
      assertValidId(id)
      const file = metaPath(dataDir, id)
      if (!existsSync(file)) return null
      const next = String(model || '').trim().slice(0, 64)
      if (!next) return null
      let meta: LocalSession
      try {
        meta = JSON.parse(readFileSync(file, 'utf-8')) as LocalSession
      } catch {
        return null
      }
      meta.model = next
      meta.updatedAt = Date.now()
      writeFileSync(file, JSON.stringify(meta, null, 2), 'utf-8')
      return meta
    },

    getSession(id) {
      assertValidId(id)
      const file = metaPath(dataDir, id)
      if (!existsSync(file)) return null
      try {
        return JSON.parse(readFileSync(file, 'utf-8')) as LocalSession
      } catch {
        return null
      }
    },

    appendMessage(id, msg) {
      assertValidId(id)
      ensureDir()
      const stored: StoredMessage = { ...msg, ts: msg.ts ?? Date.now() }
      const file = messagesPath(dataDir, id)
      // 崩溃留下的半截行先补换行，保证新消息独立成行
      ensureTrailingNewline(file)
      appendFileSync(file, JSON.stringify(stored) + '\n', 'utf-8')
      // 刷新会话 updatedAt（元数据缺失时不阻断）
      const metaFile = metaPath(dataDir, id)
      if (existsSync(metaFile)) {
        try {
          const meta = JSON.parse(readFileSync(metaFile, 'utf-8')) as LocalSession
          meta.updatedAt = stored.ts
          writeFileSync(metaFile, JSON.stringify(meta, null, 2), 'utf-8')
        } catch {
          // 元数据损坏时保持消息写入成功
        }
      }
      return stored
    },

    getMessages(id) {
      assertValidId(id)
      return readMessages(id)
    },

    buildContext(id, maxChars) {
      assertValidId(id)
      // 纵深防御: 孤儿 tool 消息(无前置 assistant.tool_calls 的 tool_call_id)直接丢弃,
      // 避免中止/崩溃残留让下次请求带出 provider 400
      const msgs = dropOrphanToolMessages(readMessages(id))
      return selectContextMessages(msgs, maxChars).kept.map(toChatMessage)
    },

    estimateContext(id, maxChars) {
      assertValidId(id)
      const msgs = dropOrphanToolMessages(readMessages(id))
      const { kept, used } = selectContextMessages(msgs, maxChars)
      return { used, kept: kept.length, total: msgs.reduce((sum, m) => sum + messageSize(m), 0) }
    },

    rewriteMessages(id, messages) {
      assertValidId(id)
      ensureDir()
      const normalized: StoredMessage[] = messages.map((m) => ({ ...m, ts: m.ts ?? Date.now() }))
      writeMessagesAtomic(id, normalized)
      touchMeta(id)
      return normalized
    },

    truncateMessages(id, keepCount) {
      assertValidId(id)
      const all = readMessages(id)
      // 边界: NaN/负数按 0, 超长按全量(损坏行已被 readMessages 跳过, 重写即清除)
      const raw = Math.floor(Number(keepCount))
      const keep = Number.isFinite(raw) ? Math.min(Math.max(0, raw), all.length) : 0
      return this.rewriteMessages(id, all.slice(0, keep))
    },

    getActiveRemoteTools(id) {
      assertValidId(id)
      return normalizeToolNames(this.getSession(id)?.activeRemoteTools)
    },

    addActiveRemoteTools(id, names) {
      assertValidId(id)
      const current = this.getActiveRemoteTools(id)
      const merged = normalizeToolNames([...current, ...names])
      // 无新增(含会话不存在时 current 恒空)则不产生写盘
      if (merged.length === current.length) return current
      return this.setActiveRemoteTools(id, merged)
    },

    setActiveRemoteTools(id, names) {
      assertValidId(id)
      const file = metaPath(dataDir, id)
      if (!existsSync(file)) return []
      let meta: LocalSession
      try {
        const parsed: unknown = JSON.parse(readFileSync(file, 'utf-8'))
        if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return []
        meta = parsed as LocalSession
      } catch {
        // meta 损坏时不覆盖原文件，返回空数组让调用方按无激活处理
        return []
      }
      const next = normalizeToolNames(names)
      if (next.length > 0) meta.activeRemoteTools = next
      else delete meta.activeRemoteTools
      writeFileSync(file, JSON.stringify(meta, null, 2), 'utf-8')
      return next
    }
  }

  /** 原子重写消息文件: 先写临时文件再 rename, 崩溃时旧文件保持完整 */
  function writeMessagesAtomic(id: string, messages: StoredMessage[]): void {
    const file = messagesPath(dataDir, id)
    const tmp = join(sessionsDir(dataDir), `.${id}.messages.${process.pid}.${randomBytes(4).toString('hex')}.tmp`)
    const body = messages.length ? messages.map((m) => JSON.stringify(m)).join('\n') + '\n' : ''
    writeFileSync(tmp, body, 'utf-8')
    try {
      renameSync(tmp, file)
    } catch (err) {
      rmSync(tmp, { force: true })
      throw err
    }
  }

  /** 刷新会话 updatedAt(元数据缺失/损坏时不阻断消息写入, 与 appendMessage 同约定) */
  function touchMeta(id: string): void {
    const metaFile = metaPath(dataDir, id)
    if (!existsSync(metaFile)) return
    try {
      const meta = JSON.parse(readFileSync(metaFile, 'utf-8')) as LocalSession
      meta.updatedAt = Date.now()
      writeFileSync(metaFile, JSON.stringify(meta, null, 2), 'utf-8')
    } catch {
      // 元数据损坏时保持消息写入成功
    }
  }

  /** 读取并解析消息文件；损坏的 jsonl 行跳过不抛 */
  function readMessages(id: string): StoredMessage[] {
    const file = messagesPath(dataDir, id)
    if (!existsSync(file)) return []
    const out: StoredMessage[] = []
    for (const line of readFileSync(file, 'utf-8').split('\n')) {
      if (!line.trim()) continue
      try {
        out.push(JSON.parse(line) as StoredMessage)
      } catch {
        // 损坏的 jsonl 行跳过不抛
      }
    }
    return out
  }
}

/**
 * 运行时模型解析: 会话级优先, 缺省回退系统设置里的默认模型。
 * 两者都为空时返回空串(由调用方拦截并提示)。
 */
export function resolveRunModel(sessionModel: string | undefined, defaultModel: string | undefined): string {
  const own = String(sessionModel ?? '').trim()
  if (own) return own
  return String(defaultModel ?? '').trim()
}

/** 解析网关 /v1/models 响应中的模型 id 列表: 过滤空值并按出现顺序去重 */
export function parseModelIds(payload: unknown): string[] {
  const data = (payload as { data?: unknown } | null)?.data
  if (!Array.isArray(data)) return []
  const out: string[] = []
  for (const item of data) {
    const id = String((item as { id?: unknown } | null)?.id ?? '').trim()
    if (id && !out.includes(id)) out.push(id)
  }
  return out
}

/**
 * 定位第 n 条用户消息在消息数组中的下标(编辑重发用)。
 * `n` 是「第几条用户消息」而非数组下标(存储里还夹着 assistant/tool 消息); 越界返回 -1。
 */
export function findNthUserMessageIndex(messages: Array<{ role: string }>, n: number): number {
  if (!Number.isInteger(n) || n < 0) return -1
  let seen = 0
  for (let i = 0; i < messages.length; i++) {
    if (messages[i].role !== 'user') continue
    if (seen === n) return i
    seen += 1
  }
  return -1
}

/** 最后一条用户消息的下标(重新生成用); 没有用户消息返回 -1 */
export function lastUserMessageIndex(messages: Array<{ role: string }>): number {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === 'user') return i
  }
  return -1
}

/**
 * 丢弃孤儿 tool 消息: toolCallId 没有前置 assistant.tool_calls 声明的 tool 消息直接过滤。
 * 中止/崩溃可能留下半截工具轮, 这种消息发给 provider 会 400（纵深防御, 只影响请求上下文, 不改存储）。
 */
export function dropOrphanToolMessages(messages: StoredMessage[]): StoredMessage[] {
  const known = new Set<string>()
  const out: StoredMessage[] = []
  for (const m of messages) {
    if (m.role === 'assistant' && m.toolCalls?.length) {
      for (const call of m.toolCalls) known.add(call.id)
      out.push(m)
      continue
    }
    if (m.role === 'tool') {
      if (!m.toolCallId || !known.has(m.toolCallId)) continue
      out.push(m)
      continue
    }
    out.push(m)
  }
  return out
}

/**
 * 清理全部临时会话(应用启动与退出时调用), 返回被删除的会话 id。
 * 临时会话在本次运行内仍走正常落盘(可回看), 仅在启动/退出时整体删除。
 */
export function removeEphemeralSessions(store: SessionStore): string[] {
  const ids = store
    .listSessions()
    .filter((s) => s.ephemeral === true)
    .map((s) => s.id)
  for (const id of ids) store.deleteSession(id)
  return ids
}
