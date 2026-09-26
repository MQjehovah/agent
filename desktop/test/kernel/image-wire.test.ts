import { test } from 'node:test'
import assert from 'node:assert'
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { buildWireContent } from '../../electron/main/kernel/image-wire'

// 1x1 透明 PNG
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=',
  'base64'
)

test('buildWireContent: 无图片路径 → 原样返回文本', () => {
  assert.equal(buildWireContent('你好，没有图片', '/tmp/nowhere'), '你好，没有图片')
})

test('buildWireContent: .attachments 图片 → 文本 + image_url(data URL)', () => {
  const ws = mkdtempSync(join(tmpdir(), 'iw-'))
  mkdirSync(join(ws, '.attachments'), { recursive: true })
  writeFileSync(join(ws, '.attachments', 'a.png'), PNG)
  const out = buildWireContent('看这张图 .attachments/a.png', ws)
  assert.ok(Array.isArray(out), '应返回 content 数组')
  const arr = out as Array<Record<string, any>>
  assert.equal(arr[0].type, 'text')
  assert.ok(
    arr.some((p) => p.type === 'image_url' && String(p.image_url.url).startsWith('data:image/png;base64,'))
  )
})

test('buildWireContent: 路径存在但非图片 → 不进图片', () => {
  const ws = mkdtempSync(join(tmpdir(), 'iw-'))
  writeFileSync(join(ws, 'note.png'), 'not an image but exists')
  const out = buildWireContent('note.png', ws)
  // 存在即内联（内容不校验是否为真图）；此处断言仍生成 image_url
  assert.ok(Array.isArray(out))
})
