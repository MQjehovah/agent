/**
 * 全局快捷键的校验与注册(不直接依赖 Electron,便于单测):
 * 只做「基本校验」——修饰键 + 单个主键,格式如 Alt+Space / Ctrl+Shift+A。
 */

export interface HotkeyDeps {
  unregisterAll(): void
  register(accelerator: string, callback: () => void): boolean
}

export interface HotkeyResult {
  ok: boolean
  error?: string
  /** true 表示未配置快捷键(功能主动关闭),区别于「注册失败」 */
  disabled?: boolean
}

const MODIFIER_RE =
  /^(ctrl|control|cmd|command|cmdorctrl|commandorcontrol|alt|option|altgr|shift|super|meta)$/i

/** 主键:字母/数字/F1-F24 或 Electron 常见命名键,不接受空白与 '+' */
const KEY_RE = /^[a-z0-9][a-z0-9_-]*$/i

/** 是否形如 `Alt+Space`、`Ctrl+Shift+A` 的合法 accelerator(要求至少一个修饰键) */
export function isValidAccelerator(accelerator: string): boolean {
  const accel = accelerator.trim()
  if (!accel || /\s/.test(accel)) return false
  const parts = accel.split('+')
  if (parts.length < 2) return false

  const key = parts[parts.length - 1]
  if (MODIFIER_RE.test(key) || !KEY_RE.test(key)) return false

  const modifiers = parts.slice(0, -1)
  const seen = new Set<string>()
  for (const part of modifiers) {
    if (!MODIFIER_RE.test(part)) return false
    const normalized = part.toLowerCase()
    if (seen.has(normalized)) return false
    seen.add(normalized)
  }
  return true
}

/**
 * 注册新快捷键(全局只保留一个):格式非法/为空时不动当前注册,只有确认要换绑才先注销。
 * 注册失败(被占用/系统拒绝)与格式非法都只降级为错误信息,不抛异常。
 */
export function registerHotkeyWith(
  deps: HotkeyDeps,
  accelerator: string,
  onTrigger: () => void
): HotkeyResult {
  const accel = accelerator.trim()
  if (!accel) {
    // 空串=主动关闭快捷键
    try {
      deps.unregisterAll()
    } catch {
      // 注销失败不影响状态判定
    }
    return { ok: false, error: '未设置快捷键' }
  }
  if (!isValidAccelerator(accel)) return { ok: false, error: `快捷键格式非法:${accel}` }
  try {
    deps.unregisterAll()
  } catch {
    // 注销失败不阻断后续注册尝试
  }
  try {
    const ok = deps.register(accel, onTrigger)
    return ok ? { ok: true } : { ok: false, error: `快捷键被占用:${accel}` }
  } catch (err) {
    return { ok: false, error: `快捷键注册失败:${(err as Error).message}` }
  }
}
