import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  summarizeForNotification,
  handleFinishNotification,
  type FinishNotificationDeps
} from '../../electron/main/notify'

test('notify-summary：多行与多余空白折成单行', () => {
  assert.equal(summarizeForNotification('第一行\n第二行\r\n第三行'), '第一行 第二行 第三行')
  assert.equal(summarizeForNotification('  前后空白  '), '前后空白')
  assert.equal(summarizeForNotification('a\t\tb'), 'a b')
})

test('notify-summary：空串与纯空白返回空串', () => {
  assert.equal(summarizeForNotification(''), '')
  assert.equal(summarizeForNotification('  \n\t '), '')
  assert.equal(summarizeForNotification(undefined as unknown as string), '')
})

test('notify-summary：超长按 max 截断并加省略号(总长不超过 max)', () => {
  const long = 'x'.repeat(200)
  const out = summarizeForNotification(long)
  assert.equal(out.length, 80)
  assert.ok(out.endsWith('…'))

  const custom = summarizeForNotification(long, 20)
  assert.equal(custom.length, 20)
  assert.ok(custom.endsWith('…'))
})

test('notify-summary：不超长时不加省略号', () => {
  const text = '短文本'
  assert.equal(summarizeForNotification(text), text)
  assert.equal(summarizeForNotification('y'.repeat(80)), 'y'.repeat(80))
})

function stubDeps(overrides: Partial<FinishNotificationDeps> = {}) {
  const events: string[] = []
  const notifications: Array<{ title: string; body: string; onClick(): void }> = []
  const warns: string[] = []
  const deps: FinishNotificationDeps = {
    enabled: true,
    sound: false,
    supported: true,
    windowFocused: false,
    showWindow: () => events.push('show'),
    openSession: (id) => events.push(`open:${id}`),
    notify: (opts) => notifications.push(opts),
    beep: () => events.push('beep'),
    warn: (message) => warns.push(message),
    ...overrides
  }
  return { deps, events, notifications, warns }
}

const PAYLOAD = { sessionId: 'web:1:abcd', title: '会话标题', summary: '回答摘要' }

test('notify：设置关闭时不发通知', () => {
  const { deps, notifications } = stubDeps({ enabled: false })
  assert.equal(handleFinishNotification(PAYLOAD, deps), false)
  assert.equal(notifications.length, 0)
})

test('notify：主窗口聚焦时不发通知', () => {
  const { deps, notifications } = stubDeps({ windowFocused: true })
  assert.equal(handleFinishNotification(PAYLOAD, deps), false)
  assert.equal(notifications.length, 0)
})

test('notify：摘要为空时不发通知', () => {
  const { deps, notifications } = stubDeps()
  assert.equal(handleFinishNotification({ ...PAYLOAD, summary: '  \n ' }, deps), false)
  assert.equal(notifications.length, 0)
})

test('notify：未聚焦且开启时发通知,正文为截断后的摘要', () => {
  const { deps, notifications } = stubDeps()
  assert.equal(handleFinishNotification({ ...PAYLOAD, summary: 'a\n'.repeat(60) }, deps), true)
  assert.equal(notifications.length, 1)
  assert.equal(notifications[0].title, '会话标题')
  assert.equal(notifications[0].body.length, 80)
  assert.ok(notifications[0].body.endsWith('…'))
})

test('notify：点击通知聚焦主窗并打开对应会话', () => {
  const { deps, events, notifications } = stubDeps()
  handleFinishNotification(PAYLOAD, deps)
  notifications[0].onClick()
  assert.deepEqual(events, ['show', 'open:web:1:abcd'])
})

test('notify：开启提示音时播放系统蜂鸣', () => {
  const { deps, events } = stubDeps({ sound: true })
  handleFinishNotification(PAYLOAD, deps)
  assert.ok(events.includes('beep'))
})

test('notify：系统不支持通知时警告并跳过,不影响返回', () => {
  const { deps, notifications, warns } = stubDeps({ supported: false })
  assert.equal(handleFinishNotification(PAYLOAD, deps), false)
  assert.equal(notifications.length, 0)
  assert.equal(warns.length, 1)
})

test('notify：标题缺失时回退默认标题', () => {
  const { deps, notifications } = stubDeps()
  handleFinishNotification({ ...PAYLOAD, title: '' }, deps)
  assert.equal(notifications[0].title, '零号员工')
})
