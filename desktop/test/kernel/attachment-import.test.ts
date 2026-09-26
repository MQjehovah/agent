import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { importAttachments, parseAttachImportPayload } from '../../electron/main/kernel/attachment-import'
import { isAllowedAttachment, sanitizeAttachmentName } from '../../electron/main/kernel/attachments'

/** 受控临时目录，测试结束即递归删除 */
function makeTemp(): string {
  return mkdtempSync(join(tmpdir(), 'dashboard-attach-'))
}

function cleanup(...dirs: string[]): void {
  for (const dir of dirs) rmSync(dir, { recursive: true, force: true })
}

test('attachment-import: 入参只认 token，渲染层自带的 paths 被忽略', () => {
  // 只给 paths 不给 token：直接拒绝，任意源路径进不来
  assert.throws(() => parseAttachImportPayload({ sessionId: 's', paths: ['C:\\Windows\\win.ini'] }), /缺少附件令牌/)
  // token + paths 同时给：paths 被丢弃，返回对象里没有 paths 字段
  const parsed = parseAttachImportPayload({ sessionId: 's', token: 'tok', paths: ['C:\\Windows\\win.ini'] })
  assert.deepEqual(parsed, { sessionId: 's', token: 'tok' })
  assert.equal(Object.prototype.hasOwnProperty.call(parsed, 'paths'), false)
  // 缺 sessionId 同样拒绝
  assert.throws(() => parseAttachImportPayload({ token: 'tok' }), /缺少会话 id/)
})

test('attachment-import: 白名单文件复制进 .attachments', () => {
  const src = makeTemp()
  const ws = makeTemp()
  try {
    writeFileSync(join(src, 'hello.txt'), 'hi')
    const res = importAttachments(join(ws, '.attachments'), [join(src, 'hello.txt')], 1000)
    assert.equal(res.skipped.length, 0)
    assert.deepEqual(res.imported.map((i) => i.name), ['1000-hello.txt'])
    assert.equal(readFileSync(join(ws, '.attachments', '1000-hello.txt'), 'utf8'), 'hi')
  } finally {
    cleanup(src, ws)
  }
})

test('attachment-import: 保留设备名 / 非白名单扩展名记 skipped', () => {
  const ws = makeTemp()
  try {
    const res = importAttachments(
      join(ws, '.attachments'),
      ['C:\\tmp\\CON.txt', 'C:\\tmp\\NUL.txt', 'C:\\tmp\\com1.txt', 'C:\\tmp\\evil.exe']
    )
    assert.equal(res.imported.length, 0)
    assert.equal(res.skipped.length, 4)
    assert.ok(res.skipped.every((s) => s.reason.includes('不支持的文件类型')), JSON.stringify(res.skipped))
  } finally {
    cleanup(ws)
  }
})

test('attachment-import: 非字符串条目记 skipped 而非静默丢弃', () => {
  const ws = makeTemp()
  try {
    const res = importAttachments(join(ws, '.attachments'), [42, null, { x: 1 }])
    assert.equal(res.imported.length, 0)
    assert.equal(res.skipped.length, 3)
    assert.ok(res.skipped.every((s) => s.reason === '无效的路径条目'), JSON.stringify(res.skipped))
  } finally {
    cleanup(ws)
  }
})

test('attachment-import: 同名冲突追加 -N 后缀，不覆盖已有附件', () => {
  const src = makeTemp()
  const ws = makeTemp()
  try {
    writeFileSync(join(src, 'a.txt'), 'one')
    const dir = join(ws, '.attachments')
    const first = importAttachments(dir, [join(src, 'a.txt')], 1000)
    const second = importAttachments(dir, [join(src, 'a.txt')], 1000)
    assert.equal(first.imported[0].name, '1000-a.txt')
    assert.equal(second.imported[0].name, '1000-a-1.txt')
    assert.equal(readFileSync(join(dir, '1000-a.txt'), 'utf8'), 'one')
    assert.equal(readFileSync(join(dir, '1000-a-1.txt'), 'utf8'), 'one')
    assert.equal(existsSync(join(dir, '1000-a.txt')), true)
  } finally {
    cleanup(src, ws)
  }
})

test('attachment-import: 扩展名按清洗后名字判定（report.txt 尾随空格应可接受）', () => {
  assert.equal(sanitizeAttachmentName('report.txt '), 'report.txt')
  assert.equal(isAllowedAttachment(sanitizeAttachmentName('report.txt ')), true)
  // 对照：直接拿原始 basename 判定会把尾随空格算进扩展名而误拒
  assert.equal(isAllowedAttachment('report.txt '), false)
})

test('attachment-import: 控制字符重组出的 .. 不进入目标名', () => {
  const safe = sanitizeAttachmentName('.\u0000.b.txt')
  assert.equal(safe, 'b.txt')
  assert.ok(!safe.includes('..'))
})

test('attachment-import: 压缩回调把 png 换成 jpg 并删除原文件', () => {
  const src = makeTemp()
  const ws = makeTemp()
  try {
    writeFileSync(join(src, 'logo.png'), 'PNGDATA')
    const dir = join(ws, '.attachments')
    const compress = (abs: string): { path: string } => {
      const out = abs.replace(/\.[^.]+$/, '') + '.jpg'
      writeFileSync(out, 'JPEGDATA')
      return { path: out }
    }
    const res = importAttachments(dir, [join(src, 'logo.png')], 2000, compress)
    assert.equal(res.skipped.length, 0)
    assert.equal(res.imported[0].name, '2000-logo.jpg')
    assert.equal(existsSync(join(dir, '2000-logo.png')), false)
    assert.equal(readFileSync(join(dir, '2000-logo.jpg'), 'utf8'), 'JPEGDATA')
  } finally {
    cleanup(src, ws)
  }
})

test('attachment-import: 压缩回调返回 null → 保留原文件', () => {
  const src = makeTemp()
  const ws = makeTemp()
  try {
    writeFileSync(join(src, 'pic.png'), 'RAW')
    const dir = join(ws, '.attachments')
    const res = importAttachments(dir, [join(src, 'pic.png')], 3000, () => null)
    assert.equal(res.imported[0].name, '3000-pic.png')
    assert.equal(readFileSync(join(dir, '3000-pic.png'), 'utf8'), 'RAW')
  } finally {
    cleanup(src, ws)
  }
})
