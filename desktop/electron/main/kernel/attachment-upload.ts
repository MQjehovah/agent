import { readFile } from 'node:fs/promises'
import { basename } from 'node:path'
import { isAllowedAttachment, MAX_ATTACHMENT_BYTES, sanitizeAttachmentName } from './attachments'
import type { AttachmentImportResult } from './attachment-import'

/**
 * 在线(agent)附件上传的纯逻辑层：把已授权的本地路径 POST 到
 * agent 的 `/api/workspace/upload`，返回与本地导入一致的形状，
 * 使渲染层「+ 文件」在两种模式下共用同一套 chip / 消息提示逻辑。
 */

export interface AgentUploadTarget {
  /** agent 服务地址(设置页 agentUrl) */
  baseUrl: string
  /** agent JWT(主进程身份,渲染层不持有) */
  token: string
}

export async function uploadAttachmentsToAgent(
  paths: string[],
  target: AgentUploadTarget,
  fetchImpl: typeof fetch = fetch
): Promise<AttachmentImportResult> {
  const imported: AttachmentImportResult['imported'] = []
  const skipped: AttachmentImportResult['skipped'] = []
  const base = target.baseUrl.replace(/\/+$/, '')

  for (const src of paths) {
    const name = sanitizeAttachmentName(basename(src))
    if (!isAllowedAttachment(name)) {
      skipped.push({ path: src, reason: '不支持的文件类型' })
      continue
    }
    try {
      const buf = await readFile(src)
      if (buf.byteLength > MAX_ATTACHMENT_BYTES) {
        skipped.push({ path: src, reason: '文件超过 20MB' })
        continue
      }
      const form = new FormData()
      form.append('file', new Blob([buf]), name)
      const res = await fetchImpl(`${base}/api/workspace/upload`, {
        method: 'POST',
        headers: { authorization: `Bearer ${target.token}` },
        body: form
      })
      if (!res.ok) {
        const text = await res.text().catch(() => '')
        let reason = `上传失败(HTTP ${res.status})`
        try {
          const parsed = JSON.parse(text) as { error?: string }
          if (parsed?.error) reason = String(parsed.error)
        } catch {
          // 保留默认原因
        }
        skipped.push({ path: src, reason })
        continue
      }
      const data = (await res.json()) as { name?: string; relPath?: string }
      if (!data.relPath) {
        skipped.push({ path: src, reason: '上传响应缺少 relPath' })
        continue
      }
      imported.push({ relPath: data.relPath, name: data.name ?? name })
    } catch (err) {
      skipped.push({ path: src, reason: (err as Error).message })
    }
  }

  return { imported, skipped }
}
