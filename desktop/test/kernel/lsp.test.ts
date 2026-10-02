import { test } from 'node:test'
import assert from 'node:assert/strict'
import { languageFor, lspTool } from '../../electron/main/kernel/lsp'

test('lsp: languageFor 按扩展名映射', () => {
  assert.equal(languageFor('a.py'), 'python')
  assert.equal(languageFor('a.ts'), 'typescript')
  assert.equal(languageFor('a.tsx'), 'typescriptreact')
  assert.equal(languageFor('a.js'), 'javascript')
  assert.equal(languageFor('a.go'), 'go')
  assert.equal(languageFor('a.rs'), 'rust')
  assert.equal(languageFor('a.unknown'), '')
})

test('lsp: 参数校验（未知 operation / 缺文件 / 不支持类型）', async () => {
  const ctx = { workspace: process.cwd(), sessionId: 's' }
  const badOp = await lspTool.execute({ operation: 'nope', file: 'x.ts' }, ctx)
  assert.equal(badOp.ok, false)
  assert.ok(badOp.output.includes('未知 operation'))

  const noFile = await lspTool.execute({ operation: 'diagnostics' }, ctx)
  assert.equal(noFile.ok, false)
  assert.ok(noFile.output.includes('file'))

  const missing = await lspTool.execute({ operation: 'diagnostics', file: 'no-such-file.ts' }, ctx)
  assert.equal(missing.ok, false)

  // 存在的非代码文件 → 不支持的类型
  const unsupported = await lspTool.execute({ operation: 'diagnostics', file: 'package.json' }, ctx)
  assert.equal(unsupported.ok, false)
  assert.ok(unsupported.output.includes('不支持的文件类型'))
})
