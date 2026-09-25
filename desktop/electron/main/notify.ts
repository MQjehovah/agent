/**
 * 系统通知的纯逻辑(不依赖 Electron,便于单测):
 * 摘要折叠/截断,以及「是否发通知」的策略与点击行为编排。
 * electron/main/index.ts 注入 Notification / shell.beep 等运行时依赖。
 */

/** 把多行文本折成单行并截断到 max 个字符(含省略号),空文本返回空串 */
export function summarizeForNotification(text: string, max = 80): string {
  const flat = String(text ?? '')
    .replace(/\s+/g, ' ')
    .trim()
  if (!flat) return ''
  if (flat.length <= max) return flat
  const keep = Math.max(0, max - 1)
  return flat.slice(0, keep) + '…'
}

export interface FinishNotificationPayload {
  sessionId: string
  title: string
  summary: string
}

export interface FinishNotificationDeps {
  /** 设置项 notifyOnFinish */
  enabled: boolean
  /** 设置项 notifySound */
  sound: boolean
  /** 系统是否支持通知 */
  supported: boolean
  /** 主窗口当前是否聚焦(聚焦则不发,避免打扰) */
  windowFocused: boolean
  showWindow(): void
  openSession(sessionId: string): void
  notify(opts: { title: string; body: string; onClick(): void }): void
  beep(): void
  warn?(message: string): void
}

/** 返回是否真正发出了通知(便于测试与排查) */
export function handleFinishNotification(
  payload: FinishNotificationPayload,
  deps: FinishNotificationDeps
): boolean {
  if (!deps.enabled) return false
  const body = summarizeForNotification(payload.summary)
  if (!body) return false
  if (deps.windowFocused) return false
  if (!deps.supported) {
    deps.warn?.('[notify] 当前系统不支持通知,已跳过')
    return false
  }
  try {
    deps.notify({
      title: payload.title || '零号员工',
      body,
      onClick: () => {
        deps.showWindow()
        deps.openSession(payload.sessionId)
      }
    })
    if (deps.sound) deps.beep()
  } catch (err) {
    deps.warn?.(`[notify] 发送通知失败:${(err as Error).message}`)
    return false
  }
  return true
}
