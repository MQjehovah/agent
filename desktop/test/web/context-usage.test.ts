import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  CONTEXT_WARN_RATIO,
  formatCharCount,
  formatContextUsage
} from '../../src/utils/context-usage'

/** 输入区上下文徽标(E)的纯逻辑: k/M 缩写、百分比、≥80% 告警级别 */

test('context-usage: 字符缩写覆盖 k/M 与去尾随 .0', () => {
  assert.equal(formatCharCount(0), '0')
  assert.equal(formatCharCount(999), '999')
  assert.equal(formatCharCount(1000), '1k')
  assert.equal(formatCharCount(12345), '12.3k')
  assert.equal(formatCharCount(96000), '96k')
  assert.equal(formatCharCount(999999), '1000k')
  assert.equal(formatCharCount(1_000_000), '1M')
  assert.equal(formatCharCount(1_234_567), '1.2M')
})

test('context-usage: 非法字符数安全归零, 不产生 NaN', () => {
  assert.equal(formatCharCount(Number.NaN), '0')
  assert.equal(formatCharCount(-5), '0')
  assert.equal(formatCharCount(Number.POSITIVE_INFINITY), '0')
})

test('context-usage: 12.3k / 96k 的完整视图与常规级别', () => {
  const view = formatContextUsage(12345, 96000)
  assert.equal(view.label, '12.3k / 96k')
  assert.equal(view.usedLabel, '12.3k')
  assert.equal(view.budgetLabel, '96k')
  assert.equal(view.percent, 13)
  assert.equal(view.level, 'normal')
})

test('context-usage: 使用率 >= 80% 触发 warn, 不足保持 normal', () => {
  assert.equal(formatContextUsage(76799, 96000).level, 'normal')
  assert.equal(formatContextUsage(76800, 96000).level, 'warn')
  assert.equal(formatContextUsage(96000, 96000).level, 'warn')
})

test('context-usage: 超预算时百分比截断到 999, 预算非法时降级为 0%', () => {
  assert.equal(formatContextUsage(96000 * 100, 96000).percent, 999)
  const zeroBudget = formatContextUsage(500, 0)
  assert.equal(zeroBudget.percent, 0)
  assert.equal(zeroBudget.level, 'normal')
  assert.equal(zeroBudget.label, '500 / 0')
  assert.equal(formatContextUsage(Number.NaN, Number.NaN).percent, 0)
})

test('context-usage: 告警阈值常量符合设计(80%)', () => {
  assert.equal(CONTEXT_WARN_RATIO, 0.8)
})
