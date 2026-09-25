import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { uploadAttachmentsToAgent } from '../../electron/main/kernel/attachment-upload'

function makeTemp(): string {
  return mkdtempSync(join(tmpdir(), 'dashboard-upload-'))
}

test('attachment-upload: 上传成功返回 relPath, 地址拼接与 Bearer 正确', async () => {
  const dir = makeTemp()
  try {
    const file = join(dir, 'note.md')
    writeFileSync(file, '# hi')
    const calls: { url: string; auth?: string }[] = []
    const fakeFetch = (async (url: string | URL, init?: RequestInit) => {
      const headers = (init?.headers ?? {}) as Record<string, string>
      calls.push({ url: String(url), auth: headers.authorization })
      return new Response(JSON.stringify({ name: 'note.md', relPath: 'uploads/1-note.md', size: 4 }), {
        status: 200,
        headers: { 'content-type': 'application/json' }
      })
    }) as unknown as typeof fetch

    const res = await uploadAttachmentsToAgent([file], { baseUrl: 'http://127.0.0.1:8090/', token: 'jwt' }, fakeFetch)
    assert.deepEqual(res.imported, [{ relPath: 'uploads/1-note.md', name: 'note.md' }])
    assert.deepEqual(res.skipped, [])
    assert.equal(calls.length, 1)
    assert.equal(calls[0].url, 'http://127.0.0.1:8090/api/workspace/upload')
    assert.equal(calls[0].auth, 'Bearer jwt')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('attachment-upload: 白名单外的扩展名跳过且不发请求', async () => {
  const dir = makeTemp()
  try {
    const file = join(dir, 'evil.exe')
    writeFileSync(file, 'MZ')
    let called = 0
    const fakeFetch = (async () => {
      called += 1
      return new Response('{}', { status: 200 })
    }) as unknown as typeof fetch

    const res = await uploadAttachmentsToAgent([file], { baseUrl: 'http://x', token: 't' }, fakeFetch)
    assert.equal(called, 0)
    assert.equal(res.imported.length, 0)
    assert.equal(res.skipped.length, 1)
    assert.match(res.skipped[0].reason, /不支持的文件类型/)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('attachment-upload: 服务端错误原因透传到 skipped', async () => {
  const dir = makeTemp()
  try {
    const file = join(dir, 'note.md')
    writeFileSync(file, '# hi')
    const fakeFetch = (async () =>
      new Response(JSON.stringify({ error: '文件超过 20MB 上限' }), {
        status: 400,
        headers: { 'content-type': 'application/json' }
      })) as unknown as typeof fetch

    const res = await uploadAttachmentsToAgent([file], { baseUrl: 'http://x', token: 't' }, fakeFetch)
    assert.equal(res.imported.length, 0)
    assert.equal(res.skipped.length, 1)
    assert.match(res.skipped[0].reason, /20MB/)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
