import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { delimiter, extname, join } from 'node:path'
import { errMessage, fail, ok, safeRealWithin, truncate } from './tools'
import { codeDiagnosticsTool } from './coding-tools'
import type { DiagnosticIssue, ToolDefinition, ToolResult } from './types'

/**
 * 最小 LSP 客户端（stdio + JSON-RPC）——给桌面内核提供「编辑器级」能力：
 * 实时诊断 / hover / 跳转定义 / 查找引用 / 重命名。
 * 语言服务器按 (工作区, 语言) 复用；未安装则优雅降级（diagnostics 回退 code_diagnostics）。
 * 语言服务器经 AGENT_LSP_SERVERS(JSON: 语言→命令数组) 覆盖，缺省用常见服务器。
 */

const LANG_BY_EXT: Record<string, string> = {
  '.py': 'python',
  '.pyi': 'python',
  '.ts': 'typescript',
  '.tsx': 'typescriptreact',
  '.js': 'javascript',
  '.jsx': 'javascriptreact',
  '.mjs': 'javascript',
  '.cjs': 'javascript',
  '.go': 'go',
  '.rs': 'rust'
}

const DEFAULT_SERVERS: Record<string, string[]> = {
  python: ['pyright-langserver', '--stdio'],
  typescript: ['typescript-language-server', '--stdio'],
  javascript: ['typescript-language-server', '--stdio'],
  typescriptreact: ['typescript-language-server', '--stdio'],
  javascriptreact: ['typescript-language-server', '--stdio'],
  go: ['gopls'],
  rust: ['rust-analyzer']
}

const SEVERITY: Record<number, string> = { 1: 'error', 2: 'warning', 3: 'info', 4: 'hint' }

export function languageFor(path: string): string {
  return LANG_BY_EXT[extname(path).toLowerCase()] || ''
}

function serverCommand(lang: string): string[] | null {
  const raw = process.env.AGENT_LSP_SERVERS
  if (raw) {
    try {
      const cfg = JSON.parse(raw) as Record<string, unknown>
      const entry = cfg?.[lang]
      if (Array.isArray(entry) && entry.every((x) => typeof x === 'string')) return entry as string[]
    } catch {
      // 解析失败忽略，用默认
    }
  }
  return DEFAULT_SERVERS[lang] ?? null
}

/** 命令是否可解析到可执行文件（含 PATH 查找），避免反复 spawn 异常 */
function commandAvailable(cmd: string): boolean {
  if (cmd.includes('/') || cmd.includes('\\')) return existsSync(cmd)
  const exts = process.platform === 'win32' ? ['', '.cmd', '.exe', '.bat', '.ps1'] : ['']
  const dirs = (process.env.PATH || '').split(delimiter).filter(Boolean)
  for (const dir of dirs) {
    for (const ext of exts) {
      if (existsSync(join(dir, cmd + ext))) return true
    }
  }
  return false
}

function pathToUri(abs: string): string {
  return 'file:///' + abs.replace(/\\/g, '/').replace(/^\/+/, '')
}

interface Pending {
  resolve: (v: unknown) => void
  reject: (e: Error) => void
  timer: NodeJS.Timeout
}

class LspClient {
  private id = 0
  private pending = new Map<number, Pending>()
  private diags = new Map<string, unknown[]>()
  private waiters = new Map<string, Array<() => void>>()
  private opened = new Set<string>()
  private buffer = Buffer.alloc(0)
  private contentLength = -1
  private closed = false

  private constructor(private proc: ChildProcessWithoutNullStreams) {
    proc.stdout.on('data', (chunk: Buffer) => this.onData(chunk))
    proc.on('close', () => this.onClose())
    proc.on('error', () => this.onClose())
  }

  static async start(command: string[], root: string): Promise<LspClient | null> {
    if (!commandAvailable(command[0])) return null
    let proc: ChildProcessWithoutNullStreams
    try {
      proc = spawn(command[0], command.slice(1), {
        cwd: root,
        stdio: ['pipe', 'pipe', 'ignore'],
        windowsHide: true
      }) as unknown as ChildProcessWithoutNullStreams
    } catch {
      return null
    }
    const client = new LspClient(proc)
    try {
      await client.request(
        'initialize',
        {
          processId: process.pid,
          rootUri: pathToUri(root),
          capabilities: { textDocument: { publishDiagnostics: {} } },
          workspaceFolders: [{ uri: pathToUri(root), name: root.split(/[\\/]/).pop() || 'root' }]
        },
        20000
      )
      client.notify('initialized', {})
      return client
    } catch {
      client.dispose()
      return null
    }
  }

  private onClose(): void {
    if (this.closed) return
    this.closed = true
    for (const p of this.pending.values()) {
      clearTimeout(p.timer)
      p.reject(new Error('LSP 连接已关闭'))
    }
    this.pending.clear()
  }

  private onData(chunk: Buffer): void {
    this.buffer = Buffer.concat([this.buffer, chunk])
    for (;;) {
      if (this.contentLength < 0) {
        const idx = this.buffer.indexOf('\r\n\r\n')
        if (idx < 0) return
        const header = this.buffer.slice(0, idx).toString('ascii')
        const m = header.match(/content-length:\s*(\d+)/i)
        this.contentLength = m ? parseInt(m[1], 10) : 0
        this.buffer = this.buffer.slice(idx + 4)
        if (!m) continue
      }
      if (this.buffer.length < this.contentLength) return
      const body = this.buffer.slice(0, this.contentLength).toString('utf8')
      this.buffer = this.buffer.slice(this.contentLength)
      this.contentLength = -1
      try {
        this.dispatch(JSON.parse(body) as Record<string, unknown>)
      } catch {
        // 忽略坏帧
      }
    }
  }

  private dispatch(msg: Record<string, unknown>): void {
    const id = msg.id as number | undefined
    if (id !== undefined && this.pending.has(id)) {
      const p = this.pending.get(id)!
      this.pending.delete(id)
      clearTimeout(p.timer)
      if (msg.error) p.reject(new Error(JSON.stringify(msg.error)))
      else p.resolve(msg.result)
      return
    }
    if (msg.method === 'textDocument/publishDiagnostics') {
      const params = (msg.params || {}) as { uri?: string; diagnostics?: unknown[] }
      const uri = params.uri || ''
      this.diags.set(uri, params.diagnostics || [])
      const ws = this.waiters.get(uri)
      if (ws) {
        this.waiters.delete(uri)
        for (const w of ws) w()
      }
    }
  }

  private write(obj: unknown): void {
    const body = Buffer.from(JSON.stringify(obj), 'utf8')
    const header = Buffer.from(`Content-Length: ${body.length}\r\n\r\n`, 'ascii')
    this.proc.stdin.write(Buffer.concat([header, body]))
  }

  private notify(method: string, params: unknown): void {
    this.write({ jsonrpc: '2.0', method, params })
  }

  private request(method: string, params: unknown, timeoutMs = 15000): Promise<unknown> {
    const rid = ++this.id
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(rid)
        reject(new Error('LSP 请求超时'))
      }, timeoutMs)
      this.pending.set(rid, { resolve, reject, timer })
      this.write({ jsonrpc: '2.0', id: rid, method, params })
    })
  }

  didOpen(path: string, text: string): string {
    const uri = pathToUri(path)
    if (!this.opened.has(uri)) {
      this.notify('textDocument/didOpen', {
        textDocument: { uri, languageId: languageFor(path), version: 1, text }
      })
      this.opened.add(uri)
    }
    return uri
  }

  async waitDiagnostics(uri: string, timeoutMs = 8000): Promise<unknown[]> {
    if (!this.diags.has(uri)) {
      await new Promise<void>((resolve) => {
        const list = this.waiters.get(uri) ?? []
        list.push(resolve)
        this.waiters.set(uri, list)
        setTimeout(() => {
          const cur = this.waiters.get(uri)
          if (cur) this.waiters.set(uri, cur.filter((f) => f !== resolve))
          resolve()
        }, timeoutMs)
      })
    }
    return this.diags.get(uri) ?? []
  }

  hover(path: string, line: number, char: number): Promise<unknown> {
    return this.request('textDocument/hover', { textDocument: { uri: pathToUri(path) }, position: pos(line, char) })
  }
  definition(path: string, line: number, char: number): Promise<unknown> {
    return this.request('textDocument/definition', { textDocument: { uri: pathToUri(path) }, position: pos(line, char) })
  }
  references(path: string, line: number, char: number): Promise<unknown> {
    return this.request('textDocument/references', {
      textDocument: { uri: pathToUri(path) },
      position: pos(line, char),
      context: { includeDeclaration: true }
    })
  }
  rename(path: string, line: number, char: number, newName: string): Promise<unknown> {
    return this.request(
      'textDocument/rename',
      { textDocument: { uri: pathToUri(path) }, position: pos(line, char), newName },
      30000
    )
  }

  dispose(): void {
    this.onClose()
    try {
      this.proc.kill()
    } catch {
      // ignore
    }
  }
}

function pos(line: number, char: number): { line: number; character: number } {
  return { line: Math.max(0, (line || 1) - 1), character: Math.max(0, (char || 1) - 1) }
}

// ---- 连接复用（按 工作区|语言） ----
const clients = new Map<string, Promise<LspClient | null>>()
const failed = new Set<string>()
const handles: LspClient[] = []

async function getClient(root: string, lang: string): Promise<LspClient | null> {
  const key = `${root}|${lang}`
  if (failed.has(key)) return null
  const existing = clients.get(key)
  if (existing) return existing
  const cmd = serverCommand(lang)
  if (!cmd) {
    failed.add(key)
    return null
  }
  const pending = (async () => {
    const client = await LspClient.start(cmd, root)
    if (!client) failed.add(key)
    else handles.push(client)
    return client
  })()
  clients.set(key, pending)
  return pending
}

/** 进程退出时关闭全部语言服务器 */
export function shutdownAllLsp(): void {
  for (const h of handles.splice(0, handles.length)) h.dispose()
  clients.clear()
  failed.clear()
}

const OPS = ['diagnostics', 'hover', 'definition', 'references', 'rename'] as const

export const lspTool: ToolDefinition = {
  name: 'lsp',
  description:
    '语言服务器(LSP)查询：operation=diagnostics 取实时诊断(错误/警告)，hover 看类型/文档，definition 跳转定义，references 查引用，rename 生成重命名编辑(new_name)。需要目标语言服务器已安装(pyright/ts-ls/gopls/rust-analyzer)或经 AGENT_LSP_SERVERS 配置；line/character 为 1-based。diagnostics 无 LSP 时回退 code_diagnostics。',
  kind: 'read',
  parameters: {
    type: 'object',
    properties: {
      operation: {
        type: 'string',
        enum: ['diagnostics', 'hover', 'definition', 'references', 'rename'],
        description: '操作类型'
      },
      file: { type: 'string', description: '目标文件（相对工作区）' },
      line: { type: 'integer', description: '行号(1-based；hover/definition/references/rename 需要)' },
      character: { type: 'integer', description: '列号(1-based，默认 1)' },
      new_name: { type: 'string', description: 'operation=rename 时的新名称' }
    },
    required: ['operation', 'file']
  },
  async execute(args, ctx): Promise<ToolResult> {
    const op = String(args.operation ?? '').toLowerCase()
    if (!OPS.includes(op as (typeof OPS)[number])) return fail(`未知 operation: ${op}`)
    const file = String(args.file ?? '').trim()
    if (!file) return fail('参数 file 必须为非空字符串')
    let real: string
    try {
      real = await safeRealWithin(ctx.workspace, file)
    } catch (e) {
      return fail(`文件不可用: ${errMessage(e)}`)
    }
    if (!existsSync(real)) return fail(`文件不存在: ${file}`)

    const lang = languageFor(real)
    if (!lang) return fail(`不支持的文件类型: ${extname(real)}`)

    const client = await getClient(ctx.workspace, lang)
    if (!client) {
      if (op === 'diagnostics') return diagnosticsFallback(ctx, file)
      return fail('未找到可用的语言服务器。请安装对应语言的 LSP(pyright/typescript-language-server/gopls/rust-analyzer)，或用 AGENT_LSP_SERVERS 指定(JSON: 语言→命令数组)。')
    }

    const line = Number(args.line) || 0
    const character = Number(args.character) || 1
    try {
      if (op === 'diagnostics') {
        const text = readFileSync(real, 'utf8')
        const uri = client.didOpen(real, text)
        const diags = (await client.waitDiagnostics(uri)) as Array<Record<string, unknown>>
        const issues = diags.map((d) => {
          const range = (d.range ?? {}) as { start?: { line?: number; character?: number } }
          return {
            line: (range.start?.line ?? 0) + 1,
            character: (range.start?.character ?? 0) + 1,
            severity: SEVERITY[Number(d.severity ?? 1)] || 'error',
            message: String(d.message ?? ''),
            source: String(d.source ?? 'lsp')
          }
        })
        return ok(
          JSON.stringify({ success: true, operation: 'diagnostics', file, issue_count: issues.length, issues }, null, 2)
        )
      }
      if (line <= 0) return fail(`operation=${op} 需要 line(1-based)`)
      if (op === 'hover') {
        return ok(truncate(JSON.stringify({ success: true, operation: op, result: await client.hover(real, line, character) }, null, 2)))
      }
      if (op === 'definition') {
        return ok(truncate(JSON.stringify({ success: true, operation: op, result: await client.definition(real, line, character) }, null, 2)))
      }
      if (op === 'references') {
        return ok(truncate(JSON.stringify({ success: true, operation: op, result: await client.references(real, line, character) }, null, 2)))
      }
      const newName = String(args.new_name ?? '').trim()
      if (!newName) return fail('operation=rename 需要 new_name')
      const edit = await client.rename(real, line, character, newName)
      return ok(truncate(JSON.stringify({ success: true, operation: 'rename', workspace_edit: edit }, null, 2)))
    } catch (e) {
      return fail(`LSP 请求失败: ${errMessage(e)}`)
    }
  }
}

/**
 * 取某文件的语言诊断（供写后诊断使用）。无 LSP 服务器/不支持的类型/出错一律返回 []，绝不抛。
 * 只做「有 LSP 才查」，不在这里 spawn 重型检查器（避免每次写后跑 tsc/ruff）。
 */
export async function lspDiagnostics(workspace: string, rel: string): Promise<DiagnosticIssue[]> {
  const lang = languageFor(rel)
  if (!lang) return []
  let real: string
  try {
    real = await safeRealWithin(workspace, rel)
  } catch {
    return []
  }
  if (!existsSync(real)) return []
  const client = await getClient(workspace, lang)
  if (!client) return []
  try {
    const text = readFileSync(real, 'utf8')
    const uri = client.didOpen(real, text)
    const diags = (await client.waitDiagnostics(uri, 5000)) as Array<Record<string, unknown>>
    return diags.map((d) => {
      const range = (d.range ?? {}) as { start?: { line?: number; character?: number } }
      return {
        line: (range.start?.line ?? 0) + 1,
        character: (range.start?.character ?? 0) + 1,
        severity: SEVERITY[Number(d.severity ?? 1)] || 'error',
        message: String(d.message ?? '')
      }
    })
  } catch {
    return []
  }
}

async function diagnosticsFallback(ctx: { workspace: string; sessionId: string }, file: string): Promise<ToolResult> {
  try {
    const result = await codeDiagnosticsTool.execute({}, ctx)
    return ok(
      JSON.stringify(
        {
          success: true,
          operation: 'diagnostics',
          fallback: 'code_diagnostics',
          note: '未找到语言服务器，已回退 code_diagnostics',
          output: result.output
        },
        null,
        2
      )
    )
  } catch (e) {
    return fail(`诊断失败: ${errMessage(e)}`)
  }
}
