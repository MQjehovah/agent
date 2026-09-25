/**
 * 输入区「上下文用量」徽标的纯逻辑（E 阶段）。
 *
 * 主进程按 buildContext 同口径给出已用/预算字符数，这里只负责展示格式化：
 * k/M 缩写、整数百分比与告警级别（≥80% 变色）。抽为纯函数便于单测。
 */

/** 使用率达到该比例即触发告警配色 */
export const CONTEXT_WARN_RATIO = 0.8

/** 百分比展示上限（预算极小/异常时不出现 4 位数百分比） */
const MAX_PERCENT = 999

export type ContextUsageLevel = 'normal' | 'warn'

export interface ContextUsageView {
  /** 已用字符缩写，如 `12.3k` */
  usedLabel: string
  /** 预算字符缩写，如 `96k` */
  budgetLabel: string
  /** `12.3k / 96k` */
  label: string
  /** 0-100 的整数百分比（预算非法时为 0） */
  percent: number
  /** 告警级别：≥80% 为 warn */
  level: ContextUsageLevel
}

/** 字符数缩写：<1000 原样，<1M 用 k，其余用 M；保留 1 位小数并去掉尾随 `.0` */
export function formatCharCount(chars: number): string {
  const n = Number.isFinite(chars) && chars > 0 ? chars : 0
  if (n < 1000) return String(Math.round(n))
  const scaled = (divisor: number): string => {
    const value = (n / divisor).toFixed(1)
    return value.endsWith('.0') ? value.slice(0, -2) : value
  }
  return n < 1_000_000 ? `${scaled(1000)}k` : `${scaled(1_000_000)}M`
}

/** 组装徽标视图；used/budget 非法时安全降级（不抛错、不出现 NaN） */
export function formatContextUsage(used: number, budget: number): ContextUsageView {
  const safeUsed = Number.isFinite(used) && used > 0 ? used : 0
  const safeBudget = Number.isFinite(budget) && budget > 0 ? budget : 0
  const percent = safeBudget > 0 ? Math.min(MAX_PERCENT, Math.round((safeUsed / safeBudget) * 100)) : 0
  return {
    usedLabel: formatCharCount(safeUsed),
    budgetLabel: formatCharCount(safeBudget),
    label: `${formatCharCount(safeUsed)} / ${formatCharCount(safeBudget)}`,
    percent,
    level: safeBudget > 0 && safeUsed / safeBudget >= CONTEXT_WARN_RATIO ? 'warn' : 'normal'
  }
}
