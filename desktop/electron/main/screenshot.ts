/**
 * 截图（E 阶段）主进程侧纯逻辑：屏幕源选择与失败文案。
 *
 * 刻意不 import electron：desktopCapturer/screen 的调用留在 index.ts 的 IPC handler，
 * 这里只保留可单测的决策与文案映射。
 */

/** desktopCapturer 屏幕源的最小形状（只需选源用到的字段） */
export interface ScreenSourceLike {
  id: string
  display_id?: string
}

/**
 * 选主屏对应的屏幕源：优先 display_id 与主屏 id 匹配，退化取第一个。
 * desktopCapturer 返回的 display_id 是字符串，screen.getPrimaryDisplay().id 是数字，故统一 String 比较。
 */
export function pickScreenSource<T extends ScreenSourceLike>(
  sources: readonly T[],
  primaryDisplayId: string | number
): T | undefined {
  if (!sources.length) return undefined
  const key = String(primaryDisplayId)
  return sources.find((s) => String(s.display_id ?? '') === key) ?? sources[0]
}

/** 把捕获链路的失败原因转成可读中文文案（渲染层直接展示） */
export function screenshotFailureText(cause: unknown): string {
  const raw = cause instanceof Error ? cause.message : typeof cause === 'string' ? cause : ''
  const msg = raw.trim()
  if (!msg) return '截图失败：未获取到屏幕画面'
  if (/permission|denied|not authorized|权限|拒绝|授权/i.test(msg)) {
    return `截图失败：无屏幕录制权限（${msg}）`
  }
  return `截图失败：${msg}`
}
