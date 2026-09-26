/**
 * 把用户消息里的图片附件路径内联为多模态 wire content（仅当前这轮；历史仍存文本路径）。
 *
 * 桌面端本地 kernel 直接调 router，需自己做多模态：识别消息里的 `.attachments/xxx.png`
 * 等图片路径，从 workspace 读取并 base64 成 data URL。找不到/超限则原样返回文本。
 */
import { existsSync, readFileSync, statSync } from 'fs'
import { basename, extname, isAbsolute, join } from 'path'

const MIME: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
  '.bmp': 'image/bmp',
  '.svg': 'image/svg+xml'
}
const MAX_BYTES = 10 * 1024 * 1024
const MAX_IMAGES = 8
const IMG_RE = /([^\s"'<>()[\]，。；、]+\.(?:png|jpe?g|webp|gif|bmp|svg))/gi

export function buildWireContent(
  text: string,
  workspace: string
): string | Array<Record<string, unknown>> {
  if (!text) return text
  const urls: string[] = []
  const seen = new Set<string>()
  const re = new RegExp(IMG_RE.source, IMG_RE.flags)
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    const tok = m[1].trim()
    const cands = isAbsolute(tok)
      ? [tok]
      : [join(workspace, tok), join(workspace, '.attachments', basename(tok))]
    const hit = cands.find((p) => {
      try {
        return existsSync(p) && statSync(p).isFile()
      } catch {
        return false
      }
    })
    if (!hit || seen.has(hit)) continue
    try {
      if (statSync(hit).size > MAX_BYTES) continue
      const b64 = readFileSync(hit).toString('base64')
      const ext = extname(hit).toLowerCase()
      urls.push(`data:${MIME[ext] || 'image/png'};base64,${b64}`)
      seen.add(hit)
    } catch {
      /* 读取失败忽略 */
    }
    if (urls.length >= MAX_IMAGES) break
  }
  if (urls.length === 0) return text
  const parts: Array<Record<string, unknown>> = []
  if (text) parts.push({ type: 'text', text })
  for (const u of urls) parts.push({ type: 'image_url', image_url: { url: u } })
  return parts
}
