import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { execFileSync } from 'node:child_process'
import {
  applyHunks,
  collectSymbols,
  detectProjectType,
  parseUnifiedDiff
} from '../../electron/main/kernel/coding-tools'
import { codingTools } from '../../electron/main/kernel/coding-tools'
import type { ToolContext } from '../../electron/main/kernel/types'

function tool(name: string) {
  const t = codingTools.find((x) => x.name === name)
  assert.ok(t, `缺少工具 ${name}`)
  return t!
}

function tmp(t: { after: (fn: () => void) => void }, prefix: string): string {
  const dir = mkdtempSync(join(tmpdir(), prefix))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  return dir
}

const ctx = (workspace: string): ToolContext => ({ workspace, sessionId: 's1' })

test('coding: parseUnifiedDiff + applyHunks 命中上下文并替换', () => {
  const diff = ['--- a/f.txt', '+++ b/f.txt', '@@ -1,3 +1,4 @@', ' line1', '-line2', '+line2x', '+line2y', ' line3'].join('\n')
  const files = parseUnifiedDiff(diff)
  assert.equal(files.length, 1)
  assert.equal(files[0].path, 'f.txt')
  const applied = applyHunks(['line1', 'line2', 'line3'], files[0].hunks)
  assert.ok(applied.ok)
  assert.deepEqual(applied.lines, ['line1', 'line2x', 'line2y', 'line3'])
})

test('coding: applyHunks 上下文不匹配时整体失败', () => {
  const diff = ['+++ b/f.txt', '@@ -1,2 +1,2 @@', ' aaa', '-bbb', '+ccc'].join('\n')
  const files = parseUnifiedDiff(diff)
  const applied = applyHunks(['xxx', 'bbb'], files[0].hunks)
  assert.equal(applied.ok, false)
})

test('coding: patch 工具落盘、dry_run 不写、冲突整体拒绝', async (t) => {
  const dir = tmp(t, 'patch-')
  writeFileSync(join(dir, 'f.txt'), 'line1\nline2\nline3')
  const diff = ['--- a/f.txt', '+++ b/f.txt', '@@ -1,3 +1,3 @@', ' line1', '-line2', '+LINE2', ' line3'].join('\n')

  const dry = await tool('patch').execute({ diff, dry_run: true }, ctx(dir))
  assert.equal(dry.ok, true)
  assert.equal(readFileSync(join(dir, 'f.txt'), 'utf8'), 'line1\nline2\nline3', 'dry_run 不应写盘')

  const applied = await tool('patch').execute({ diff }, ctx(dir))
  assert.equal(applied.ok, true, applied.output)
  assert.equal(readFileSync(join(dir, 'f.txt'), 'utf8'), 'line1\nLINE2\nline3')

  const conflict = ['+++ b/f.txt', '@@ -1,1 +1,1 @@', '-NOPE', '+X'].join('\n')
  const res = await tool('patch').execute({ diff: conflict }, ctx(dir))
  assert.equal(res.ok, false)
  assert.equal(readFileSync(join(dir, 'f.txt'), 'utf8'), 'line1\nLINE2\nline3', '失败不应改动')
})

test('coding: collectSymbols 提取 TS/Python 符号', () => {
  const ts = ['export function foo() {}', 'class Bar {}', 'export interface Baz {}', 'const qux = () => {}'].join('\n')
  assert.deepEqual(collectSymbols('a.ts', ts), [
    { line: 1, kind: 'function', name: 'foo' },
    { line: 2, kind: 'class', name: 'Bar' },
    { line: 3, kind: 'interface', name: 'Baz' },
    { line: 4, kind: 'const', name: 'qux' }
  ])
  const py = ['def run():', '    pass', 'class Svc:'].join('\n')
  assert.deepEqual(collectSymbols('a.py', py), [
    { line: 1, kind: 'def', name: 'run' },
    { line: 3, kind: 'class', name: 'Svc' }
  ])
})

test('coding: detectProjectType 按标志文件判定', () => {
  assert.equal(detectProjectType((r) => r === 'package.json'), 'node')
  assert.equal(detectProjectType((r) => r === 'pyproject.toml'), 'python')
  assert.equal(detectProjectType((r) => r === 'go.mod'), 'go')
  assert.equal(detectProjectType((r) => r === 'Cargo.toml'), 'rust')
  assert.equal(detectProjectType(() => false), 'unknown')
})

test('coding: rename_symbol 整词重命名 + dry_run 预览', async (t) => {
  const dir = tmp(t, 'rename-')
  mkdirSync(join(dir, 'src'), { recursive: true })
  writeFileSync(join(dir, 'src', 'a.ts'), 'const foo = 1\nfoo + foobar\n')
  writeFileSync(join(dir, 'src', 'b.ts'), 'foo()\n')

  const dry = await tool('rename_symbol').execute(
    { symbol: 'foo', new_name: 'bar', include: '*.ts', dry_run: true },
    ctx(dir)
  )
  assert.equal(dry.ok, true)
  assert.ok(dry.output.includes('预演'), dry.output)
  assert.equal(readFileSync(join(dir, 'src', 'a.ts'), 'utf8'), 'const foo = 1\nfoo + foobar\n')

  const run = await tool('rename_symbol').execute({ symbol: 'foo', new_name: 'bar', include: '*.ts' }, ctx(dir))
  assert.equal(run.ok, true, run.output)
  // 整词：foobar 不应被改
  assert.equal(readFileSync(join(dir, 'src', 'a.ts'), 'utf8'), 'const bar = 1\nbar + foobar\n')
  assert.equal(readFileSync(join(dir, 'src', 'b.ts'), 'utf8'), 'bar()\n')
})

test('coding: git 工具 status/commit/log 走临时仓库', async (t) => {
  const dir = tmp(t, 'git-')
  try {
    execFileSync('git', ['init'], { cwd: dir, stdio: 'ignore' })
  } catch {
    t.skip?.('git 不可用，跳过')
    return
  }
  execFileSync('git', ['config', 'user.email', 't@t.dev'], { cwd: dir })
  execFileSync('git', ['config', 'user.name', 'tester'], { cwd: dir })
  writeFileSync(join(dir, 'a.txt'), 'hello\n')

  const status = await tool('git').execute({ operation: 'status' }, ctx(dir))
  assert.equal(status.ok, true)
  assert.ok(status.output.includes('a.txt'), status.output)

  const commit = await tool('git').execute({ operation: 'commit', message: 'init' }, ctx(dir))
  assert.equal(commit.ok, true, commit.output)
  const log = await tool('git').execute({ operation: 'log' }, ctx(dir))
  assert.equal(log.ok, true)
  assert.ok(log.output.includes('init'), log.output)
  assert.ok(existsSync(join(dir, '.git')))
})
