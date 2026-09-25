import { exec } from 'node:child_process'
import { mkdir, readFile, readdir, realpath, stat, writeFile } from 'node:fs/promises'
import { basename, dirname, join } from 'node:path'
import { isWithin, resolveWithin, PathEscapeError } from './pathsafe'
import type { ToolContext, ToolDefinition, ToolResult } from './types'

/**
 * 内置工具集：file_read / file_write / file_edit / terminal / glob / grep。
 * 面向 LLM 调用：一切失败（参数非法、路径越界、fs 异常）都折叠为 {ok:false, output:原因}，
 * 绝不向调用方抛未处理异常；输出文本尽量自包含，便于模型据此自我修正。
 */

/** 单个工具输出的最大字符数，超出截断防止撑爆上下文 */
const MAX_OUTPUT = 10 * 1024
/** grep 单次返回的最大匹配行数 */
const MAX_GREP_LINES = 200
/** grep 单文件读取上限，避免扫描超大/二进制文件 */
const MAX_GREP_FILE_SIZE = 1024 * 1024
/** 目录遍历时跳过的目录名：依赖目录与版本库内部对象对 LLM 无价值且体量巨大 */
const SKIP_DIRS = new Set(['node_modules', '.git'])

function ok(output: string): ToolResult {
  return { ok: true, output }
}

function fail(output: string): ToolResult {
  return { ok: false, output }
}

/** 统一把未知异常转成可读字符串 */
function errMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

/** 超长输出截断，附原始长度提示 */
function truncate(text: string, max = MAX_OUTPUT): string {
  if (text.length <= max) return text
  return text.slice(0, max) + `\n…[输出过长已截断，原始长度 ${text.length} 字符]`
}

/** 参数取非空字符串，非法返回 null（由各工具自行 fail） */
function strArg(args: Record<string, unknown>, key: string): string | null {
  const v = args[key]
  return typeof v === 'string' && v.length > 0 ? v : null
}

/**
 * 解析路径并校验「真实路径」位于工作区内（防符号链接逃逸）：
 * 1) 字符串级 resolveWithin 拦截 .. 等越界；
 * 2) 对目标做 fs.realpath——若目标不存在（ENOENT）则向上找最近的已存在祖先做 realpath，
 *    再 lexical 拼回缺失段；
 * 3) 与「真实化的 workspace」做 isWithin 比较（workspace 本身也可能是符号链接路径）。
 * 通过校验后返回可用于 fs 操作的真实绝对路径；任何越界抛 PathEscapeError。
 */
async function safeRealWithin(workspace: string, target: string): Promise<string> {
  const abs = resolveWithin(workspace, target)
  const realWs = await realpath(workspace)
  let probe = abs
  const missing: string[] = []
  for (;;) {
    try {
      const realBase = await realpath(probe)
      const real = missing.length > 0 ? join(realBase, ...missing) : realBase
      if (!isWithin(realWs, real)) {
        throw new PathEscapeError(`路径越界: ${target} 经符号链接解析到工作区外`)
      }
      return real
    } catch (e) {
      if (e instanceof PathEscapeError) throw e
      // 目标（或中间目录）不存在：记下缺失段，向上一级继续解析
      const parent = dirname(probe)
      if (parent === probe) throw e
      missing.unshift(basename(probe))
      probe = parent
    }
  }
}

/**
 * 递归遍历工作区，回调收到「/ 分隔的相对路径」与绝对路径。
 * 不进入 SKIP_DIRS，不跟随符号链接目录/文件（Dirent 的 isDirectory/isFile 对链接均为 false），
 * 从源头保证 glob/grep 不会借符号链接逃逸。
 */
async function walkFiles(
  root: string,
  visit: (rel: string, abs: string) => Promise<void> | void
): Promise<void> {
  async function walkDir(dir: string, relDir: string): Promise<void> {
    let entries
    try {
      entries = await readdir(dir, { withFileTypes: true })
    } catch {
      return // 无权限/已消失的目录直接跳过
    }
    for (const ent of entries) {
      if (SKIP_DIRS.has(ent.name)) continue
      const rel = relDir ? `${relDir}/${ent.name}` : ent.name
      const abs = join(dir, ent.name)
      if (ent.isDirectory() && !ent.isSymbolicLink()) {
        await walkDir(abs, rel)
      } else if (ent.isFile() && !ent.isSymbolicLink()) {
        await visit(rel, abs)
      }
    }
  }
  await walkDir(root, '')
}

/** 通配符转正则：** 跨目录段，* 与 ? 不跨 /；其余字符按字面量处理 */
function globToRegex(pattern: string): RegExp {
  let re = ''
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i]
    if (c === '*') {
      if (pattern[i + 1] === '*') {
        i++ // 消费第二个 *
        if (pattern[i + 1] === '/') {
          i++ // '** /' 可匹配零层或多层目录
          re += '(?:.*/)?'
        } else {
          re += '.*'
        }
      } else {
        re += '[^/]*'
      }
    } else if (c === '?') {
      re += '[^/]'
    } else {
      re += c.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    }
  }
  return new RegExp(`^${re}$`)
}

/** glob 匹配：无 / 的模式退化为对文件名匹配（如 *.ts 命中任意深度） */
function globMatch(re: RegExp, pattern: string, rel: string): boolean {
  return re.test(rel) || (!pattern.includes('/') && re.test(basename(rel)))
}

/** 正则字面量转义，用于 grep 非法正则时退化为字面量搜索 */
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// ---- kb_search: 公司知识库检索 ----
// 工具本身保持「纯」：不经手 identity/upstream，调 RAG 的能力由 ipc 层注入 callRag。

/** RAG /api/search 响应里单条 results 的字段（全部容错，可能缺失） */
export interface RagSearchHit {
  id?: string
  title?: string
  content?: string
  source?: string
}

/** kb_search 结果折叠后的最大输出长度 */
const KB_RESULT_MAX = 4 * 1024

/** 把单条命中折叠为「【出处】标题\n摘要」；字段缺失/类型错直接留空，不抛 */
function formatKbHit(raw: unknown): string {
  if (typeof raw !== 'object' || raw === null) return ''
  const hit = raw as Record<string, unknown>
  const title = typeof hit.title === 'string' && hit.title.trim() ? hit.title.trim() : ''
  const content = typeof hit.content === 'string' && hit.content.trim() ? hit.content.trim() : ''
  const source = typeof hit.source === 'string' && hit.source.trim() ? hit.source.trim() : 'unknown'
  // 无标题也无摘要的纯占位命中直接丢弃，不产出「【出处】」空块
  if (!title && !content) return ''
  let block = `【${source}】${title}`
  if (content) block += `\n${content}`
  return block
}

/**
 * 把 RAG /api/search 的响应折叠为人类可读文本（纯函数，便于单测）：
 * results 缺失/非数组/条目异常一律按空处理；无命中给中文提示；超长截断到 ~4KB。
 */
export function formatKbResults(payload: unknown): string {
  const obj = (typeof payload === 'object' && payload !== null ? payload : {}) as { results?: unknown }
  const list = Array.isArray(obj.results) ? (obj.results as RagSearchHit[]) : []
  const blocks = list.map(formatKbHit).filter((b) => b.length > 0)
  if (blocks.length === 0) return '知识库无相关结果'
  return truncate(blocks.join('\n\n'), KB_RESULT_MAX)
}

/** 构造 kb_search 工具；callRag 由 ipc 层注入（直连 RAG 并带 OIDC token），tools 不碰身份逻辑 */
export function kbSearchTool(callRag: (path: string, body: unknown) => Promise<unknown>): ToolDefinition {
  return {
    name: 'kb_search',
    description:
      '检索公司知识库(报销制度/制度文档/维基等企业资料)，返回相关条目的标题、摘要与出处，用于回答企业内部制度与流程类问题。',
    kind: 'read',
    parameters: {
      type: 'object',
      properties: {
        query: { type: 'string', description: '检索关键词或自然语言问题' },
        top_k: { type: 'integer', description: '最多返回的结果条数(1-10)，缺省 5' }
      },
      required: ['query']
    },
    async execute(args) {
      try {
        const query = strArg(args, 'query')
        if (!query) return fail('参数 query 必须为非空字符串')
        const raw = args.top_k
        const topK =
          typeof raw === 'number' && Number.isFinite(raw) ? Math.min(Math.max(Math.floor(raw), 1), 10) : 5
        const payload = await callRag('/api/search', { query, top_k: topK })
        return ok(formatKbResults(payload))
      } catch (e) {
        return fail(`知识库检索失败: ${errMessage(e)}`)
      }
    }
  }
}

export const builtinTools: ToolDefinition[] = [
  {
    name: 'file_read',
    description:
      '读取工作区内文本文件（utf-8）。路径相对工作区根目录，支持按行偏移与行数限制分段读取大文件。路径越界或文件不存在会返回失败原因。',
    kind: 'read',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: '文件路径，相对工作区根目录' },
        offset: { type: 'integer', description: '起始行号（从 1 开始），缺省从第 1 行读取' },
        limit: { type: 'integer', description: '最多读取的行数，缺省不限制' }
      },
      required: ['path']
    },
    async execute(args, ctx) {
      try {
        const path = strArg(args, 'path')
        if (!path) return fail('参数 path 必须为非空字符串')
        const real = await safeRealWithin(ctx.workspace, path)
        const content = await readFile(real, 'utf-8')
        let lines = content.split(/\r?\n/)
        const offset = typeof args.offset === 'number' && args.offset >= 1 ? Math.floor(args.offset) : 1
        const limit = typeof args.limit === 'number' && args.limit >= 1 ? Math.floor(args.limit) : Infinity
        if (offset > 1) lines = lines.slice(offset - 1)
        if (Number.isFinite(limit)) lines = lines.slice(0, limit)
        return ok(truncate(lines.join('\n')))
      } catch (e) {
        return fail(`读取失败: ${errMessage(e)}`)
      }
    }
  },
  {
    name: 'file_write',
    description:
      '在工作区内写入文本文件（utf-8，整文件覆盖），父目录不存在会自动创建。只能写工作区内路径；经符号链接指向工作区外的路径会被拒绝。',
    kind: 'write',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: '文件路径，相对工作区根目录' },
        content: { type: 'string', description: '要写入的完整文本内容' }
      },
      required: ['path', 'content']
    },
    async execute(args, ctx) {
      try {
        const path = strArg(args, 'path')
        if (!path) return fail('参数 path 必须为非空字符串')
        const content = typeof args.content === 'string' ? args.content : null
        if (content === null) return fail('参数 content 必须为字符串')
        const real = await safeRealWithin(ctx.workspace, path)
        await mkdir(dirname(real), { recursive: true })
        await writeFile(real, content, 'utf-8')
        return ok(`已写入 ${path}（${Buffer.byteLength(content, 'utf-8')} 字节）`)
      } catch (e) {
        return fail(`写入失败: ${errMessage(e)}`)
      }
    }
  },
  {
    name: 'file_edit',
    description:
      '对工作区内文本文件做精确字符串替换（先读全文，替换后整文件写回）。old 必须与文件内容精确匹配（含空白），未命中即失败。all 为 true 时替换全部出现，否则只替换第一处。',
    kind: 'write',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: '文件路径，相对工作区根目录' },
        old: { type: 'string', description: '要被替换的精确文本，不能为空' },
        new: { type: 'string', description: '替换后的文本' },
        all: { type: 'boolean', description: '是否替换全部出现，缺省只替换第一处' }
      },
      required: ['path', 'old', 'new']
    },
    async execute(args, ctx) {
      try {
        const path = strArg(args, 'path')
        if (!path) return fail('参数 path 必须为非空字符串')
        const oldText = typeof args.old === 'string' ? args.old : null
        if (oldText === null || oldText.length === 0) return fail('参数 old 必须为非空字符串')
        const newText = typeof args.new === 'string' ? args.new : null
        if (newText === null) return fail('参数 new 必须为字符串')
        const real = await safeRealWithin(ctx.workspace, path)
        const content = await readFile(real, 'utf-8')
        if (!content.includes(oldText)) return fail(`未找到目标文本: ${oldText.slice(0, 100)}`)
        const next = args.all === true ? content.replaceAll(oldText, newText) : content.replace(oldText, newText)
        await writeFile(real, next, 'utf-8')
        return ok(`已更新 ${path}`)
      } catch (e) {
        return fail(`编辑失败: ${errMessage(e)}`)
      }
    }
  },
  {
    name: 'terminal',
    description:
      '在工作区根目录执行终端命令并返回输出。Windows 下经 cmd /c 执行。stdout 与 stderr 分开标注；退出码非零或超时会标记失败但输出仍会返回。输出超长会被截断。',
    kind: 'write',
    parameters: {
      type: 'object',
      properties: {
        command: { type: 'string', description: '要执行的命令' },
        timeoutSec: { type: 'integer', description: '超时秒数，缺省 60' }
      },
      required: ['command']
    },
    async execute(args, ctx) {
      try {
        const command = strArg(args, 'command')
        if (!command) return fail('参数 command 必须为非空字符串')
        const timeoutSec =
          typeof args.timeoutSec === 'number' && args.timeoutSec > 0 ? args.timeoutSec : 60
        const cmd = process.platform === 'win32' ? `cmd /c ${command}` : command
        return await new Promise<ToolResult>(resolve => {
          exec(
            cmd,
            {
              cwd: ctx.workspace,
              timeout: timeoutSec * 1000,
              maxBuffer: 1024 * 1024,
              windowsHide: true
            },
            (err, stdout, stderr) => {
              const parts: string[] = []
              if (stdout) parts.push(`[stdout]\n${stdout}`)
              if (stderr) parts.push(`[stderr]\n${stderr}`)
              const output = parts.length > 0 ? parts.join('\n') : '(无输出)'
              if (err) {
                const reason = err.killed
                  ? `命令超时（${timeoutSec}s）被终止`
                  : `命令退出码非零: ${err.code ?? err.message}`
                resolve(fail(truncate(`${reason}\n${output}`)))
              } else {
                resolve(ok(truncate(output)))
              }
            }
          )
        })
      } catch (e) {
        return fail(`命令执行失败: ${errMessage(e)}`)
      }
    }
  },
  {
    name: 'glob',
    description:
      '按通配符模式列出工作区内文件，返回相对路径列表（换行分隔）。* 匹配单层文件名、** 跨目录层级；pattern 不含 / 时对任意深度的文件名匹配。默认排除 node_modules 与 .git。',
    kind: 'read',
    parameters: {
      type: 'object',
      properties: {
        pattern: { type: 'string', description: '通配符模式，如 **/*.ts 或 package.json' }
      },
      required: ['pattern']
    },
    async execute(args, ctx) {
      try {
        const pattern = strArg(args, 'pattern')
        if (!pattern) return fail('参数 pattern 必须为非空字符串')
        const re = globToRegex(pattern)
        const hits: string[] = []
        let truncated = false
        await walkFiles(ctx.workspace, rel => {
          if (hits.length >= MAX_GREP_LINES) {
            truncated = true
            return
          }
          if (globMatch(re, pattern, rel)) hits.push(rel)
        })
        const suffix = truncated ? `\n…[结果已截断，仅显示前 ${MAX_GREP_LINES} 条]` : ''
        return ok(hits.length > 0 ? truncate(hits.join('\n')) + suffix : '(无匹配文件)')
      } catch (e) {
        return fail(`glob 失败: ${errMessage(e)}`)
      }
    }
  },
  {
    name: 'grep',
    description:
      '在工作区文件内容中按行搜索，输出「相对路径:行号: 行内容」。pattern 优先按正则解释，非法正则退化为字面量搜索；include 可用通配符限定文件范围（如 *.ts）。默认跳过 node_modules 与 .git。',
    kind: 'read',
    parameters: {
      type: 'object',
      properties: {
        pattern: { type: 'string', description: '正则表达式或字面量文本' },
        include: { type: 'string', description: '可选的文件名过滤通配符，如 *.ts' }
      },
      required: ['pattern']
    },
    async execute(args, ctx) {
      try {
        const pattern = strArg(args, 'pattern')
        if (!pattern) return fail('参数 pattern 必须为非空字符串')
        let regex: RegExp
        try {
          regex = new RegExp(pattern)
        } catch {
          regex = new RegExp(escapeRegExp(pattern))
        }
        const include = typeof args.include === 'string' && args.include.length > 0 ? args.include : null
        const includeRe = include ? globToRegex(include) : null
        const lines: string[] = []
        let truncated = false
        await walkFiles(ctx.workspace, async (rel, abs) => {
          if (lines.length >= MAX_GREP_LINES) {
            truncated = true
            return
          }
          if (includeRe && !globMatch(includeRe, include as string, rel)) return
          try {
            const s = await stat(abs)
            if (s.size > MAX_GREP_FILE_SIZE) return
            const content = await readFile(abs, 'utf-8')
            const fileLines = content.split(/\r?\n/)
            for (let i = 0; i < fileLines.length; i++) {
              if (lines.length >= MAX_GREP_LINES) {
                truncated = true
                break
              }
              if (regex.test(fileLines[i])) lines.push(`${rel}:${i + 1}: ${fileLines[i].slice(0, 500)}`)
            }
          } catch {
            // 读取失败的文件（权限/二进制等）直接跳过
          }
        })
        let output = lines.length > 0 ? lines.join('\n') : '(无匹配)'
        if (truncated) output += `\n…[匹配结果超过 ${MAX_GREP_LINES} 行已截断]`
        return ok(truncate(output))
      } catch (e) {
        return fail(`搜索失败: ${errMessage(e)}`)
      }
    }
  }
]
