export const DINGTALK_GROUP_PREFIX = 'dingtalk_group:'

export interface ChannelMeta {
  label: string
  type: 'primary' | 'success' | 'warning' | 'info'
}

/** 钉钉群共享根判定：会话 id 以 dingtalk_group: 前缀开头（前缀即类型，见 2026-09-08 群模型）。 */
export function isDingtalkGroupSession(id: string): boolean {
  return typeof id === 'string' && id.startsWith(DINGTALK_GROUP_PREFIX)
}

/** 群根 dingtalk_group:{cid子串}:{hash}:{rand} → 取可读的 cid 子串作显示名（后端暂无群名字段）。 */
export function dingtalkGroupDisplayName(sessionId: string): string {
  const rest = isDingtalkGroupSession(sessionId)
    ? sessionId.slice(DINGTALK_GROUP_PREFIX.length)
    : sessionId
  const seg = rest.split(':', 1)[0]
  return seg || rest
}

/** 会话渠道徽标元数据。钉钉群聊与钉钉单聊视觉区分：群聊 label 显式标「钉钉群」。 */
export function channelMeta(ch?: string, id = ''): ChannelMeta {
  if (isDingtalkGroupSession(id)) return { label: '钉钉群', type: 'success' }
  switch (ch) {
    case 'web': return { label: 'Web', type: 'primary' }
    case 'dingtalk': return { label: '钉钉', type: 'success' }
    case 'feishu': return { label: '飞书', type: 'warning' }
    default: return { label: ch || '其他', type: 'info' }
  }
}
