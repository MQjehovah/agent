import { exec, execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { existsSync, readFileSync, statSync } from 'node:fs'
import { readFile, writeFile } from 'node:fs/promises'
import { extname } from 'node:path'
import { globMatch, globToRegex, ok, fail, errMessage, truncate, safeRealWithin, walkFiles } from './tools'
import type { ToolDefinition, ToolResult } from './types'

/**
 * 本地编码底座工具(离线)：patch / git / repo_map / rename_symbol / code_diagnostics。
 * 与 tools.ts 同约定：一切失败折叠为 {ok:false, output}，不抛。
 */

const execFileAsync = promisify(execFile)

/** 单文件体量上限：超过则跳过内容类处理(rename/repo_map/diagnostics 扫描) */
const MAX_SCAN_FILE = 512 * 1024
/** rename_symbol 单次改动文件数上限 */
const MAX_RENAME_FILES = 200

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// ---- patch: unified diff ----

export interface DiffHunk {
  oldStart: number
  oldCount: number
  newStart: number
  newCount: number
  lines: Array<{ kind: ' ' | '-' | '+'; text: string }>
}

export interface DiffFile {
  path: string
  hunks: DiffHunk[]
}

/** 从 `+++ b/foo.ts\t(timestamp)` 提取工作区相对路径；/dev/null 返回空串 */
function parseDiffPath(raw: string): string {
  let p = raw.trim()
  const tab = p.indexOf('\t')
  if (tab >= 0) p = p.slice(0, tab)
  if (p === '/dev/null') return ''
  if (p.startsWith('a/') || p.startsWith('b/')) p = p.slice(2)
  return p
}

/** 解析标准 unified diff 为文件/块结构(纯函数，供 patch 工具与单测) */
export function parseUnifiedDiff(diff: string): DiffFile[] {
  const lines = diff.replace(/\r\n/g, '\n').split('\n')
  const files: DiffFile[] = []
  let current: DiffFile | null = null
  let hunk: DiffHunk | null = null
  for (const line of lines) {
    if (line.startsWith('diff --git')) {
      current = null
      hunk = null
      continue
    }
    if (line.startsWith('--- ')) {
      hunk = null
      continue
    }
    if (line.startsWith('+++ ')) {
      const path = parseDiffPath(line.slice(4))
      current = path ? { path, hunks: [] } : null
      if (current) files.push(current)
      hunk = null
      continue
    }
    const m = line.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/)
    if (m) {
      if (!current) continue
      hunk = {
        oldStart: Number(m[1]),
        oldCount: m[2] === undefined ? 1 : Number(m[2]),
        newStart: Number(m[3]),
        newCount: m[4] === undefined ? 1 : Number(m[4]),
        lines: []
      }
      current.hunks.push(hunk)
      continue
    }
    if (hunk) {
      if (line.startsWith('\\')) continue // \ No newline at end of file
      const kind = line[0]
      if (kind === ' ' || kind === '-' || kind === '+') hunk.lines.push({ kind, text: line.slice(1) })
      else if (line === '') hunk.lines.push({ kind: ' ', text: '' })
      else hunk = null
    }
  }
  return files.filter((f) => f.hunks.length > 0)
}

/** 把 hunks 依次套用到文件行数组；任一块上下文不匹配即整体失败（原子） */
export function applyHunks(
  fileLines: string[],
  hunks: DiffHunk[]
): { ok: true; lines: string[] } | { ok: false; error: string } {
  const out: string[] = []
  let cursor = 0
  for (const hunk of hunks) {
    const target = hunk.oldStart > 0 ? hunk.oldStart - 1 : 0
    if (target < cursor || target > fileLines.length) {
      return { ok: false, error: `补丁定位越界: @@ -${hunk.oldStart}` }
    }
    for (let i = cursor; i < target; i++) out.push(fileLines[i])
    cursor = target
    for (const l of hunk.lines) {
      if (l.kind === ' ' || l.kind === '-') {
        if (cursor >= fileLines.length || fileLines[cursor] !== l.text) {
          return {
            ok: false,
            error: `补丁上下文不匹配(第 ${cursor + 1} 行): 期望「${l.text}」，实际「${fileLines[cursor] ?? '<EOF>'}」`
          }
        }
        if (l.kind === ' ') out.push(fileLines[cursor])
        cursor++
      } else {
        out.push(l.text)
      }
    }
  }
  for (let i = cursor; i < fileLines.length; i++) out.push(fileLines[i])
  return { ok: true, lines: out }
}

const patchTool: ToolDefinition = {
  name: 'patch',
  description:
    '用标准 unified diff(git diff 格式，含 ---/+++/@@ 头)原子地修改工作区文件。所有文件/代码块必须全部匹配成功，否则整体拒绝、不改任何文件。传 dry_run=true 只预演不落盘。',
  kind: 'write',
  parameters: {
    type: 'object',
    properties: {
      diff: { type: 'string', description: '标准 unified diff 文本' },
      dry_run: { type: 'boolean', description: '仅预演不写盘，缺省 false' }
    },
    required: ['diff']
  },
  async execute(args, ctx) {
    try {
      const diff = typeof args.diff === 'string' ? args.diff : ''
      if (!diff.trim()) return fail('参数 diff 必须为非空字符串')
      const files = parseUnifiedDiff(diff)
      if (!files.length) return fail('未解析到任何补丁块（需要 ---/+++/@@ 头）')
      const dryRun = args.dry_run === true
      const planned: Array<{ path: string; real: string; lines: string[]; added: number; removed: number }> = []
      for (const file of files) {
        const real = await safeRealWithin(ctx.workspace, file.path)
        let content = ''
        try {
          content = await readFile(real, 'utf-8')
        } catch {
          // 新文件允许不存在
        }
        const before = content.length ? content.replace(/\r\n/g, '\n').split('\n') : []
        const applied = applyHunks(before, file.hunks)
        if (!applied.ok) return fail(`${file.path}: ${applied.error}`)
        let added = 0
        let removed = 0
        for (const h of file.hunks) for (const l of h.lines) (l.kind === '+' ? added++ : l.kind === '-' ? removed++ : 0)
        planned.push({ path: file.path, real, lines: applied.lines, added, removed })
      }
      if (dryRun) {
        const summary = planned.map((p) => `${p.path}: +${p.added} -${p.removed}`).join('\n')
        return ok(`预演通过（未写入）：\n${summary}`)
      }
      for (const p of planned) await writeFile(p.real, p.lines.join('\n'), 'utf-8')
      const summary = planned.map((p) => `${p.path}: +${p.added} -${p.removed}`).join('\n')
      return ok(`已应用补丁：\n${summary}`)
    } catch (e) {
      return fail(`应用补丁失败: ${errMessage(e)}`)
    }
  }
}

// ---- git ----

async function runGit(cwd: string, args: string[], timeoutSec = 60): Promise<ToolResult> {
  try {
    const { stdout, stderr } = await execFileAsync('git', args, {
      cwd,
      timeout: timeoutSec * 1000,
      windowsHide: true,
      maxBuffer: 1024 * 1024
    })
    const out = [stdout, stderr].filter(Boolean).join('\n').trim()
    return ok(truncate(out || '(无输出)'))
  } catch (e) {
    const err = e as { stdout?: string; stderr?: string; message?: string }
    const body = [err.stdout, err.stderr].filter(Boolean).join('\n').trim()
    return fail(truncate(body || errMessage(e)))
  }
}

const gitTool: ToolDefinition = {
  name: 'git',
  description:
    '在工作区执行 git 操作。operations: status(状态)、diff(差异, 可 staged/path)、log(提交历史)、commit(暂存全部并提交 message)、checkpoint(暂存全部并打检查点)、rollback(硬回退到 ref，破坏性)。',
  kind: 'write',
  parameters: {
    type: 'object',
    properties: {
      operation: { type: 'string', enum: ['status', 'diff', 'log', 'commit', 'checkpoint', 'rollback'] },
      message: { type: 'string', description: 'commit/checkpoint 的提交信息' },
      staged: { type: 'boolean', description: 'diff 是否查看已暂存改动' },
      path: { type: 'string', description: 'diff 限定的路径(相对工作区)' },
      limit: { type: 'integer', description: 'log 返回条数，缺省 20' },
      ref: { type: 'string', description: 'rollback 目标引用，缺省 HEAD' }
    },
    required: ['operation']
  },
  async execute(args, ctx) {
    try {
      const op = String(args.operation ?? '')
      if (op === 'status') return runGit(ctx.workspace, ['status', '--porcelain=v1', '--branch'])
      if (op === 'diff') {
        const gitArgs = ['diff']
        if (args.staged === true) gitArgs.push('--staged')
        if (typeof args.path === 'string' && args.path) gitArgs.push('--', args.path)
        return runGit(ctx.workspace, gitArgs)
      }
      if (op === 'log') {
        const limit = typeof args.limit === 'number' && args.limit > 0 ? Math.floor(args.limit) : 20
        return runGit(ctx.workspace, ['log', '--oneline', '-n', String(limit)])
      }
      if (op === 'commit' || op === 'checkpoint') {
        const message =
          typeof args.message === 'string' && args.message.trim()
            ? args.message.trim()
            : `checkpoint ${new Date().toISOString()}`
        const add = await runGit(ctx.workspace, ['add', '-A'])
        if (!add.ok) return add
        return runGit(ctx.workspace, ['commit', '-m', message])
      }
      if (op === 'rollback') {
        const ref = typeof args.ref === 'string' && args.ref.trim() ? args.ref.trim() : 'HEAD'
        return runGit(ctx.workspace, ['reset', '--hard', ref])
      }
      return fail(`未知 operation: ${op}`)
    } catch (e) {
      return fail(`git 失败: ${errMessage(e)}`)
    }
  }
}

// ---- repo_map ----

export interface RepoMapSymbol {
  line: number
  kind: string
  name: string
}

/** 按扩展名用正则提取符号定义(纯函数，供 repo_map 与单测) */
export function collectSymbols(rel: string, content: string): RepoMapSymbol[] {
  const ext = extname(rel).toLowerCase()
  const lines = content.split(/\r?\n/)
  const out: RepoMapSymbol[] = []
  const push = (i: number, kind: string, name: string): void => {
    if (name) out.push({ line: i + 1, kind, name })
  }
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (ext === '.py') {
      let m = line.match(/^\s*def\s+([A-Za-z_]\w*)/)
      if (m) push(i, 'def', m[1])
      else if ((m = line.match(/^\s*class\s+([A-Za-z_]\w*)/))) push(i, 'class', m[1])
    } else if (['.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs'].includes(ext)) {
      let m = line.match(/^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)/)
      if (m) push(i, 'function', m[1])
      else if ((m = line.match(/^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)/))) push(i, 'class', m[1])
      else if ((m = line.match(/^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)/))) push(i, 'interface', m[1])
      else if ((m = line.match(/^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*[=<]/))) push(i, 'type', m[1])
      else if ((m = line.match(/^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(|function\b)/))) push(i, 'const', m[1])
      else if ((m = line.match(/^\s*(?:export\s+)?enum\s+([A-Za-z_$][\w$]*)/))) push(i, 'enum', m[1])
    } else if (ext === '.go') {
      const m = line.match(/^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)/)
      if (m) push(i, 'func', m[1])
    }
  }
  return out
}

const repoMapTool: ToolDefinition = {
  name: 'repo_map',
  description:
    '生成工作区源码的符号地图(函数/类/接口/类型等定义位置)，输出「相对路径:行号: 种类 名称」。可用 include 通配符(如 *.ts)限定范围。默认跳过 node_modules 与 .git。',
  kind: 'read',
  parameters: {
    type: 'object',
    properties: {
      include: { type: 'string', description: '文件名过滤通配符，如 *.ts 或 src/**/*.py' }
    }
  },
  async execute(args, ctx) {
    try {
      const include = typeof args.include === 'string' && args.include ? args.include : null
      const includeRe = include ? globToRegex(include) : null
      const lines: string[] = []
      await walkFiles(ctx.workspace, (rel, abs) => {
        if (includeRe && !globMatch(includeRe, include as string, rel)) return
        try {
          if (statSync(abs).size > MAX_SCAN_FILE) return
          const content = readFileSync(abs, 'utf-8')
          for (const s of collectSymbols(rel, content)) lines.push(`${rel}:${s.line}: ${s.kind} ${s.name}`)
        } catch {
          // 二进制/无权限文件跳过
        }
      })
      return ok(lines.length ? truncate(lines.join('\n')) : '(未发现符号)')
    } catch (e) {
      return fail(`repo_map 失败: ${errMessage(e)}`)
    }
  }
}

// ---- rename_symbol ----

const renameSymbolTool: ToolDefinition = {
  name: 'rename_symbol',
  description:
    '在工作区按整词把 symbol 重命名为 new_name(正则 \\b 边界)。可用 include 通配符限定文件范围；dry_run=true 只预览改动不写盘。返回每个文件的替换次数。',
  kind: 'write',
  parameters: {
    type: 'object',
    properties: {
      symbol: { type: 'string', description: '要重命名的标识符' },
      new_name: { type: 'string', description: '新名称' },
      include: { type: 'string', description: '文件名过滤通配符，如 *.ts' },
      dry_run: { type: 'boolean', description: '仅预览不写盘' }
    },
    required: ['symbol', 'new_name']
  },
  async execute(args, ctx) {
    try {
      const symbol = typeof args.symbol === 'string' ? args.symbol.trim() : ''
      if (!symbol) return fail('参数 symbol 必须为非空字符串')
      const newName = typeof args.new_name === 'string' ? args.new_name.trim() : ''
      if (!newName) return fail('参数 new_name 必须为非空字符串')
      if (!/^[A-Za-z_$][\w$]*$/.test(newName)) return fail('new_name 必须是合法标识符')
      const include = typeof args.include === 'string' && args.include ? args.include : null
      const includeRe = include ? globToRegex(include) : null
      const re = new RegExp(`\\b${escapeRegExp(symbol)}\\b`, 'g')
      const dryRun = args.dry_run === true
      const changes: Array<{ path: string; real: string; next: string; count: number }> = []
      await walkFiles(ctx.workspace, (rel, abs) => {
        if (changes.length >= MAX_RENAME_FILES) return
        if (includeRe && !globMatch(includeRe, include as string, rel)) return
        try {
          if (statSync(abs).size > MAX_SCAN_FILE) return
          const content = readFileSync(abs, 'utf-8')
          const count = (content.match(re) ?? []).length
          if (count > 0) changes.push({ path: rel, real: abs, next: content.replace(re, newName), count })
        } catch {
          // 跳过无法读取的文件
        }
      })
      if (!changes.length) return ok(`未找到标识符 ${symbol}`)
      if (!dryRun) {
        for (const c of changes) await writeFile(c.real, c.next, 'utf-8')
      }
      const total = changes.reduce((s, c) => s + c.count, 0)
      const detail = changes.map((c) => `${c.path}: ${c.count}`).join('\n')
      return ok(`${dryRun ? '预演（未写入）' : '已重命名'} ${symbol} → ${newName}（${total} 处/${changes.length} 文件）：\n${detail}`)
    } catch (e) {
      return fail(`rename_symbol 失败: ${errMessage(e)}`)
    }
  }
}

// ---- code_diagnostics ----

export type ProjectKind = 'node' | 'python' | 'go' | 'rust' | 'unknown'

/** 依据项目标志文件判定工程类型(纯函数，供单测) */
export function detectProjectType(has: (rel: string) => boolean): ProjectKind {
  if (has('tsconfig.json') || has('package.json')) return 'node'
  if (has('pyproject.toml') || has('requirements.txt') || has('setup.py')) return 'python'
  if (has('go.mod')) return 'go'
  if (has('Cargo.toml')) return 'rust'
  return 'unknown'
}

function runShell(cwd: string, command: string, timeoutSec: number): Promise<ToolResult> {
  return new Promise((resolve) => {
    exec(
      process.platform === 'win32' ? `cmd /c ${command}` : command,
      { cwd, timeout: timeoutSec * 1000, maxBuffer: 1024 * 1024, windowsHide: true },
      (err, stdout, stderr) => {
        const out = [stdout, stderr].filter(Boolean).join('\n').trim()
        if (err) resolve(fail(truncate((out ? out + '\n' : '') + `命令失败: ${err.message}`)))
        else resolve(ok(truncate(out || '(无诊断输出)')))
      }
    )
  })
}

export const codeDiagnosticsTool: ToolDefinition = {
  name: 'code_diagnostics',
  description:
    '对工作区工程做静态检查，按工程类型自动选择命令(Node: tsc --noEmit；Python: ruff check；Go: go vet；Rust: cargo check)。返回诊断输出(截断)。',
  kind: 'read',
  parameters: {
    type: 'object',
    properties: {
      timeoutSec: { type: 'integer', description: '超时秒数，缺省 120' }
    }
  },
  async execute(args, ctx) {
    try {
      const kind = detectProjectType((rel) => existsSync(`${ctx.workspace}/${rel}`))
      const timeoutSec = typeof args.timeoutSec === 'number' && args.timeoutSec > 0 ? args.timeoutSec : 120
      if (kind === 'node') {
        return runShell(ctx.workspace, existsSync(`${ctx.workspace}/tsconfig.json`) ? 'npx --no-install tsc --noEmit' : 'npx --no-install eslint .', timeoutSec)
      }
      if (kind === 'python') return runShell(ctx.workspace, 'ruff check .', timeoutSec)
      if (kind === 'go') return runShell(ctx.workspace, 'go vet ./...', timeoutSec)
      if (kind === 'rust') return runShell(ctx.workspace, 'cargo check', timeoutSec)
      return ok('未识别工程类型(缺 tsconfig.json/package.json/pyproject.toml/go.mod/Cargo.toml)，跳过诊断')
    } catch (e) {
      return fail(`诊断失败: ${errMessage(e)}`)
    }
  }
}

export const codingTools: ToolDefinition[] = [
  patchTool,
  gitTool,
  repoMapTool,
  renameSymbolTool,
  codeDiagnosticsTool
]
