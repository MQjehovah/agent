import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  isAllowedAttachment,
  attachmentTargetName,
  sanitizeAttachmentName,
  formatAttachmentNotice,
  MAX_ATTACHMENT_BYTES
} from '../../electron/main/kernel/attachments'
import { formatAttachmentNotice as rendererFormatAttachmentNotice, canSendMessage } from '../../src/utils/attachments'

const ALLOWED = [
  'a.txt',
  'a.md',
  'a.json',
  'a.csv',
  'a.py',
  'a.ts',
  'a.js',
  'a.go',
  'a.java',
  'a.sh',
  'a.yaml',
  'a.yml',
  'a.log',
  'a.pdf',
  'a.png',
  'a.jpg',
  'a.jpeg',
  'a.gif',
  'a.webp',
  'a.docx',
  'a.xlsx',
  'a.pptx'
]

test('attachments: 白名单扩展名全部放行', () => {
  for (const name of ALLOWED) {
    assert.equal(isAllowedAttachment(name), true, name)
  }
})

test('attachments: 非白名单扩展名 / 无扩展名 / 空名拒绝', () => {
  assert.equal(isAllowedAttachment('evil.exe'), false)
  assert.equal(isAllowedAttachment('lib.dll'), false)
  assert.equal(isAllowedAttachment('README'), false)
  assert.equal(isAllowedAttachment(''), false)
})

test('attachments: 含 .. 或路径分隔符的名字拒绝', () => {
  assert.equal(isAllowedAttachment('../a.txt'), false)
  assert.equal(isAllowedAttachment('..\\a.txt'), false)
  assert.equal(isAllowedAttachment('dir/a.txt'), false)
  assert.equal(isAllowedAttachment('dir\\a.txt'), false)
  assert.equal(isAllowedAttachment('..'), false)
})

test('attachments: 目标名带时间戳前缀并保留扩展名', () => {
  assert.equal(attachmentTargetName('报告.txt', 1732000000000), '1732000000000-报告.txt')
})

test('attachments: 恶意名剥离路径且扩展名策略仍生效', () => {
  const passwd = attachmentTargetName('../../etc/passwd', 1732000000000)
  assert.equal(passwd, '1732000000000-passwd')
  assert.ok(!/[\\/]/.test(passwd))
  assert.ok(!passwd.includes('..'))
  assert.equal(isAllowedAttachment(sanitizeAttachmentName('../../etc/passwd')), false)

  const evil = attachmentTargetName('..\\..\\evil.exe', 1732000000000)
  assert.equal(evil, '1732000000000-evil.exe')
  assert.ok(!/[\\/]/.test(evil))
  assert.ok(!evil.includes('..'))
  assert.equal(isAllowedAttachment(sanitizeAttachmentName('..\\..\\evil.exe')), false)

  const report = attachmentTargetName('../../secret/report.txt', 1732000000000)
  assert.equal(report, '1732000000000-report.txt')
  assert.ok(!/[\\/]/.test(report))
  assert.ok(!report.includes('..'))
  assert.equal(isAllowedAttachment(sanitizeAttachmentName('../../secret/report.txt')), true)
})

test('attachments: 大小上限为 20MB', () => {
  assert.equal(MAX_ATTACHMENT_BYTES, 20 * 1024 * 1024)
})

test('attachments: 控制字符剥离后不得重组出 .. （.\u0000.b.txt）', () => {
  assert.equal(sanitizeAttachmentName('.\u0000.b.txt'), 'b.txt')
  assert.ok(!sanitizeAttachmentName('.\u0000.b.txt').includes('..'))
  // 清洗结果扩展名仍有效，可被放行
  assert.equal(isAllowedAttachment(sanitizeAttachmentName('.\u0000.b.txt')), true)
})

test('attachments: Windows 保留设备名拒绝（CON/NUL/com1 等）', () => {
  assert.equal(isAllowedAttachment('CON.txt'), false)
  assert.equal(isAllowedAttachment('NUL.txt'), false)
  assert.equal(isAllowedAttachment('com1.txt'), false)
  assert.equal(isAllowedAttachment('LPT9.log'), false)
  assert.equal(isAllowedAttachment('nul'), false)
  // 正常名不被误伤
  assert.equal(isAllowedAttachment('console.txt'), true)
})

test('attachments: 附件提示文案格式', () => {
  assert.equal(formatAttachmentNotice([]), '')
  assert.equal(
    formatAttachmentNotice(['.attachments/1-a.txt', '.attachments/2-b.md']),
    '\n\n已附带文件：\n- .attachments/1-a.txt\n- .attachments/2-b.md'
  )
})

test('attachments: 主进程与渲染层 formatAttachmentNotice 输出完全一致', () => {
  const cases: string[][] = [
    [],
    ['a.txt'],
    ['.attachments/1-a.txt', '.attachments/2-b.md'],
    ['中文 文件.txt']
  ]
  for (const relPaths of cases) {
    assert.equal(rendererFormatAttachmentNotice(relPaths), formatAttachmentNotice(relPaths), JSON.stringify(relPaths))
  }
})

test('attachments: 仅有附件、无文本时也可发送（Fix 2）', () => {
  assert.equal(canSendMessage('', 1, false), true)
  assert.equal(canSendMessage('   ', 1, false), true)
  assert.equal(canSendMessage('hi', 0, false), true)
  assert.equal(canSendMessage('', 0, false), false)
  // 流式中一律不可发送
  assert.equal(canSendMessage('hi', 0, true), false)
  assert.equal(canSendMessage('', 1, true), false)
})
