/**
 * 截图（E 阶段）渲染层纯逻辑：dataURL 解码为字节、附件文件名与失败文案降级。
 *
 * 主进程 `desktop:screenshot` 返回 PNG dataURL；这里只做无副作用转换，
 * 落盘/导入复用既有 `localagent:file:paste` 令牌通路。
 */

export type ScreenshotResult = { ok: true; dataUrl: string } | { ok: false; error: string }

/** PNG dataURL → 字节；非 base64 或格式损坏时抛可读错误 */
export function dataUrlToBytes(dataUrl: string): Uint8Array {
  const raw = String(dataUrl ?? '')
  const comma = raw.indexOf(',')
  if (comma < 0) throw new Error('截图数据无效')
  const meta = raw.slice(0, comma)
  if (!/;base64/i.test(meta)) throw new Error('截图数据格式不支持（仅支持 base64）')
  let binary: string
  try {
    binary = atob(raw.slice(comma + 1))
  } catch {
    throw new Error('截图数据解码失败')
  }
  const out = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i)
  return out
}

/** 截图附件文件名：screenshot-YYYYMMDD-HHmmss.png */
export function screenshotFileName(ts: number): string {
  const d = new Date(ts)
  const p = (n: number): string => String(n).padStart(2, '0')
  return `screenshot-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(
    d.getMinutes()
  )}${p(d.getSeconds())}.png`
}

/** 截图失败时给用户的最终文案（主进程已给可读 error，这里只兜底） */
export function screenshotFailureMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) return `截图失败：${error.message.trim()}`
  if (typeof error === 'string' && error.trim()) return `截图失败：${error.trim()}`
  return '截图失败：未获取到屏幕画面'
}
