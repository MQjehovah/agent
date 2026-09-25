/**
 * 快速提问快捷键的渲染层基本校验(保存前即时提示)。
 * 规则与主进程 electron/main/hotkey.ts 的 isValidAccelerator 保持一致;
 * 主进程才是最终权威(被占用等注册失败由 app:hotkey:status 返回)。
 */
const MODIFIER_RE =
  /^(ctrl|control|cmd|command|cmdorctrl|commandorcontrol|alt|option|altgr|shift|super|meta)$/i
const KEY_RE = /^[a-z0-9][a-z0-9_-]*$/i

export function isValidAccelerator(accelerator: string): boolean {
  const accel = accelerator.trim()
  if (!accel || /\s/.test(accel)) return false
  const parts = accel.split('+')
  if (parts.length < 2) return false

  const key = parts[parts.length - 1]
  if (MODIFIER_RE.test(key) || !KEY_RE.test(key)) return false

  const seen = new Set<string>()
  for (const part of parts.slice(0, -1)) {
    if (!MODIFIER_RE.test(part)) return false
    const normalized = part.toLowerCase()
    if (seen.has(normalized)) return false
    seen.add(normalized)
  }
  return true
}
