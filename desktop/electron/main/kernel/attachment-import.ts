import { constants, copyFileSync, mkdirSync, statSync, unlinkSync } from 'node:fs'
import { basename, extname } from 'node:path'
import {
  attachmentTargetName,
  isAllowedAttachment,
  MAX_ATTACHMENT_BYTES,
  sanitizeAttachmentName
} from './attachments'
import { resolveWithin } from './pathsafe'

/**
 * `attach:import` 的纯逻辑层（不依赖 Electron，可单测）：
 *   - 解析入参只认 { sessionId, token }，渲染层自带的 paths 被彻底忽略；
 *   - 把 token 已解析出的源路径复制进会话工作区 `.attachments/`。
 * 源路径的授权由 attachment-tokens 的令牌存储保证。
 */

export interface AttachmentImportResult {
  imported: { relPath: string; name: string }[]
  skipped: { path: string; reason: string }[]
}

/**
 * 可选图片压缩回调（注入以保持本模块可在无 Electron 的 node 下单测）：
 * 入参为已复制到 `.attachments/` 的绝对路径；返回新生成的压缩文件绝对路径（如转为 .jpg），
 * 或 null 表示不压缩/无需压缩（保留原文件）。
 */
export type AttachmentCompressor = (absPath: string) => { path: string } | null

/** 解析 attach:import 入参：只取 sessionId + token，其余字段（尤其 paths）一律丢弃 */
export function parseAttachImportPayload(payload: unknown): { sessionId: string; token: string } {
  const p = (payload ?? {}) as { sessionId?: unknown; token?: unknown }
  const sessionId = typeof p.sessionId === 'string' ? p.sessionId : ''
  if (!sessionId) throw new Error('缺少会话 id')
  const token = typeof p.token === 'string' ? p.token.trim() : ''
  if (!token) throw new Error('缺少附件令牌，请重新选择文件')
  return { sessionId, token }
}

/** 同名冲突时的重试次数：原名之外再试 -1..-5 */
const COPY_ATTEMPTS = 5

/**
 * 以 COPYFILE_EXCL 落盘，避免同毫秒 / 跨批次同名静默覆盖已有附件。
 * EEXIST 时追加 -1..-5 后缀重试，仍冲突则抛出由调用方记入 skipped。
 */
function copyExclusive(src: string, dir: string, name: string): string {
  const ext = extname(name)
  const stem = ext ? name.slice(0, -ext.length) : name
  let lastErr: unknown
  for (let attempt = 0; attempt <= COPY_ATTEMPTS; attempt++) {
    const candidate = attempt === 0 ? name : `${stem}-${attempt}${ext}`
    const target = resolveWithin(dir, candidate)
    try {
      copyFileSync(src, target, constants.COPYFILE_EXCL)
      return candidate
    } catch (err) {
      if ((err as NodeJS.ErrnoException | undefined)?.code !== 'EEXIST') throw err
      lastErr = err
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error('目标文件已存在')
}

/**
 * 把已授权的源路径复制进 `.attachments/`。
 * 逐文件容错：扩展名/类型/大小/复制任一失败只记 skipped，不中断整批。
 */
export function importAttachments(
  dir: string,
  paths: readonly unknown[],
  baseTs: number = Date.now(),
  compress?: AttachmentCompressor
): AttachmentImportResult {
  mkdirSync(dir, { recursive: true })
  const imported: { relPath: string; name: string }[] = []
  const skipped: { path: string; reason: string }[] = []

  for (let i = 0; i < paths.length; i++) {
    const src = paths[i]
    // 路径列表由令牌解析而来，理论上必为 string；仍保留防御，异常条目记 skipped 而非静默丢弃
    if (typeof src !== 'string') {
      skipped.push({ path: String(src), reason: '无效的路径条目' })
      continue
    }
    try {
      const original = basename(src)
      // 扩展名在清洗后的名字上判定：`report.txt `（尾随空格）清洗后应被接受
      const safeName = sanitizeAttachmentName(original)
      if (!isAllowedAttachment(safeName)) {
        const ext = extname(safeName)
        skipped.push({ path: src, reason: ext ? `不支持的文件类型 ${ext}` : '不支持的文件类型（缺少扩展名）' })
        continue
      }
      const stat = statSync(src)
      if (!stat.isFile()) {
        skipped.push({ path: src, reason: '不是普通文件' })
        continue
      }
      if (stat.size > MAX_ATTACHMENT_BYTES) {
        skipped.push({ path: src, reason: `超过 ${MAX_ATTACHMENT_BYTES / 1024 / 1024}MB 上限` })
        continue
      }
      // 同批用序号错开时间戳；若仍撞名由 copyExclusive 追加后缀
      const base = attachmentTargetName(original, baseTs + i)
      let name = copyExclusive(src, dir, base)
      // 图片压缩：生成更小的 .jpg 并替换（失败保留原图，不影响导入）
      if (compress) {
        try {
          const out = compress(resolveWithin(dir, name))
          if (out && out.path) {
            const newName = basename(out.path)
            if (newName !== name) {
              try {
                unlinkSync(resolveWithin(dir, name))
              } catch {
                /* 旧文件删不掉也无妨 */
              }
              name = newName
            }
          }
        } catch {
          /* 压缩异常保留原图 */
        }
      }
      imported.push({ relPath: `.attachments/${name}`, name })
    } catch (err) {
      skipped.push({ path: src, reason: err instanceof Error ? err.message : '复制失败' })
    }
  }
  return { imported, skipped }
}
