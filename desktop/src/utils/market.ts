/** 能力市场展示映射(与 market 前端 format.js 对齐) */

export const TYPE_LABELS: Record<string, string> = {
  agent: '专家',
  tool: '代码函数',
  skill: '技能',
  mcp: '连接器',
  workflow: '能力编排',
  plugin: '能力包',
  rule: '规则',
  command: '命令',
  hook: 'Hooks'
}

export const TYPE_LETTER: Record<string, string> = {
  agent: 'A',
  tool: 'T',
  skill: 'S',
  mcp: 'M',
  workflow: 'W',
  plugin: 'P',
  rule: 'R',
  command: 'C',
  hook: 'H'
}

export const TYPE_COLORS: Record<string, string> = {
  agent: '#2f6bff',
  tool: '#12b76a',
  skill: '#f5a524',
  mcp: '#7c3aed',
  workflow: '#0ea5e9',
  plugin: '#e5484d',
  rule: '#0f766e',
  command: '#c2410c',
  hook: '#4f46e5'
}

export function typeLetter(t: string): string {
  return TYPE_LETTER[t] || '?'
}
export function typeColor(t: string): string {
  return TYPE_COLORS[t] || '#7c3aed'
}
export function typeName(t: string): string {
  return TYPE_LABELS[t] || t || '未知'
}

export function distName(d?: string): string {
  if (d === 'local') return '本地部署'
  if (d === 'remote') return '云端部署'
  return '云端 + 本地'
}
export function distBadgeCls(d?: string): string {
  if (d === 'local') return 'badge'
  if (d === 'remote') return 'badge-primary'
  return 'badge-success'
}
export function policyName(p?: string): string {
  const v = p || 'optional'
  if (v === 'default_on') return '默认加入'
  if (v === 'required') return '必装'
  return '可选加入'
}

export interface Grade {
  label: string
  cls: string
}
/** 质量分级(按评分推导;无评分为「新品」) */
export function gradeOf(cap: { rating_count?: number; avg_rating?: number }): Grade {
  const count = Number(cap.rating_count || 0)
  const avg = Number(cap.avg_rating || 0)
  if (!count) return { label: '新品', cls: 'badge' }
  if (avg >= 4.5) return { label: 'A · 优质', cls: 'badge-success' }
  if (avg >= 4) return { label: 'B · 良好', cls: 'badge-primary' }
  if (avg >= 3) return { label: 'C · 合格', cls: 'badge-warning' }
  return { label: '待评估', cls: 'badge' }
}

/** 图标占位样式（类型色） */
export function iconPhStyle(t: string): Record<string, string> {
  const color = typeColor(t)
  return { color, borderColor: color + '55', background: color + '14' }
}
