/**
 * 当前用户画像行（注入本地 agent 的系统提示词）：
 * 让本地会话能识别「我/我的」所指的用户，不再重复询问用户已提供的信息。
 * 纯函数、可离线单测；无身份信息时返回空串（调用方据此决定是否注入）。
 */

export interface UserProfileInput {
  /** 工号（SSO sub） */
  id?: string
  /** 显示名 */
  name?: string
  /** 部门（可选） */
  department?: string
  /** 钉钉 userId（可选，SSO dingtalk claim） */
  dingtalk?: string
}

const GUIDE_LINE =
  '回答"我/我的"相关问题时以该用户为准；不要向该用户询问他自己已提供的信息。'

/** 单行展平：换行/连续空白压成单个空格并去首尾，防提示词结构注入 */
function flatten(value: unknown): string {
  return String(value ?? '')
    .replace(/\s+/g, ' ')
    .trim()
}

export function buildUserProfileLine(user?: UserProfileInput): string {
  const id = flatten(user?.id)
  const name = flatten(user?.name)
  const department = flatten(user?.department)
  const dingtalk = flatten(user?.dingtalk)
  const shown = name || id
  if (!shown) return ''
  let line = `当前用户：${shown}`
  if (name && id) line += `（工号 ${id}）`
  if (department) line += `；部门：${department}`
  if (dingtalk) line += `；钉钉 userId：${dingtalk}`
  return `${line}。${GUIDE_LINE}`
}
