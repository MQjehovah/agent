import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  permissionMemoryFilePath,
  loadAlwaysAllowed,
  listAlwaysAllowed,
  addAlwaysAllowed,
  removeAlwaysAllowed,
  clearAlwaysAllowed
} from '../../electron/main/kernel/permission-memory'

function makeDataDir(t: { after(fn: () => void): void }, prefix = 'permmem-'): string {
  const dir = mkdtempSync(join(tmpdir(), prefix))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  return dir
}

/** 直接写原始记忆内容（自动建 localagent 目录），用于损坏/形状非法等容错场景 */
function writeRaw(dataDir: string, content: string): void {
  mkdirSync(join(dataDir, 'localagent'), { recursive: true })
  writeFileSync(permissionMemoryFilePath(dataDir), content)
}

test('permission-memory: 文件缺失时 load/list 返回空', (t) => {
  const dataDir = makeDataDir(t)
  assert.deepEqual([...loadAlwaysAllowed(dataDir)], [])
  assert.deepEqual(listAlwaysAllowed(dataDir), [])
})

test('permission-memory: add 后 load/list 含该工具（读写往返）', (t) => {
  const dataDir = makeDataDir(t)
  addAlwaysAllowed(dataDir, 'mcp__remote_terminal__send_command')
  assert.deepEqual([...loadAlwaysAllowed(dataDir)], ['mcp__remote_terminal__send_command'])
  addAlwaysAllowed(dataDir, 'terminal')
  assert.deepEqual(listAlwaysAllowed(dataDir), ['mcp__remote_terminal__send_command', 'terminal'])
})

test('permission-memory: 落盘形状为 { alwaysAllow: [...] }（排序去重、JSON 可读）', (t) => {
  const dataDir = makeDataDir(t)
  addAlwaysAllowed(dataDir, 'zeta')
  addAlwaysAllowed(dataDir, 'alpha')
  const raw = readFileSync(permissionMemoryFilePath(dataDir), 'utf8')
  assert.deepEqual(JSON.parse(raw) as unknown, { alwaysAllow: ['alpha', 'zeta'] })
})

test('permission-memory: 重复 add 同一工具幂等且文件内不重复', (t) => {
  const dataDir = makeDataDir(t)
  addAlwaysAllowed(dataDir, 'terminal')
  addAlwaysAllowed(dataDir, 'terminal')
  addAlwaysAllowed(dataDir, 'terminal')
  const parsed = JSON.parse(readFileSync(permissionMemoryFilePath(dataDir), 'utf8')) as { alwaysAllow: string[] }
  assert.deepEqual(parsed.alwaysAllow, ['terminal'])
})

test('permission-memory: add trim 工具名，remove 亦按规范名匹配', (t) => {
  const dataDir = makeDataDir(t)
  addAlwaysAllowed(dataDir, '  terminal  ')
  assert.deepEqual(listAlwaysAllowed(dataDir), ['terminal'])
  removeAlwaysAllowed(dataDir, ' terminal ')
  assert.deepEqual(listAlwaysAllowed(dataDir), [])
})

test('permission-memory: remove 移除并落盘，对未记住项幂等', (t) => {
  const dataDir = makeDataDir(t)
  addAlwaysAllowed(dataDir, 'a')
  addAlwaysAllowed(dataDir, 'b')
  removeAlwaysAllowed(dataDir, 'a')
  assert.deepEqual(listAlwaysAllowed(dataDir), ['b'])
  // 不存在项：不抛错、不改变现有记忆
  removeAlwaysAllowed(dataDir, 'a')
  assert.deepEqual(listAlwaysAllowed(dataDir), ['b'])
})

test('permission-memory: clear 清空全部（文件保留且 alwaysAllow: []）', (t) => {
  const dataDir = makeDataDir(t)
  addAlwaysAllowed(dataDir, 'a')
  addAlwaysAllowed(dataDir, 'b')
  clearAlwaysAllowed(dataDir)
  assert.deepEqual(listAlwaysAllowed(dataDir), [])
  const parsed = JSON.parse(readFileSync(permissionMemoryFilePath(dataDir), 'utf8')) as unknown
  assert.deepEqual(parsed, { alwaysAllow: [] })
})

test('permission-memory: 原子写不留 .tmp，目录内只有记忆文件', (t) => {
  const dataDir = makeDataDir(t)
  addAlwaysAllowed(dataDir, 'terminal')
  addAlwaysAllowed(dataDir, 'terminal')
  const entries = readdirSync(join(dataDir, 'localagent'))
  assert.deepEqual(entries, ['permission-memory.json'])
})

test('permission-memory: add/remove 拒绝非法工具名（空/超长/控制字符），且不落盘', (t) => {
  const dataDir = makeDataDir(t)
  const bads: unknown[] = ['', '   ', 'x'.repeat(129), 'bad\u0000name', 'bad\nname', 42, null]
  for (const bad of bads) {
    assert.throws(() => addAlwaysAllowed(dataDir, bad as string), /非法工具名/)
    assert.throws(() => removeAlwaysAllowed(dataDir, bad as string), /非法工具名/)
  }
  assert.ok(!existsSync(join(dataDir, 'localagent')), '非法名称不应产生任何文件')
})

test('permission-memory: JSON 损坏时 load 返回空并 console.warn', (t) => {
  const dataDir = makeDataDir(t)
  writeRaw(dataDir, '{broken json')
  const orig = console.warn
  const warns: string[] = []
  console.warn = (m: string) => warns.push(String(m))
  try {
    assert.deepEqual([...loadAlwaysAllowed(dataDir)], [])
  } finally {
    console.warn = orig
  }
  assert.ok(warns.length >= 1, '损坏时应告警一次')
  assert.ok(warns[0].includes('损坏'), warns[0])
})

test('permission-memory: 顶层非对象 / alwaysAllow 非数组/缺失时 load 返回空并告警', (t) => {
  const dataDir = makeDataDir(t)
  const orig = console.warn
  const warns: string[] = []
  console.warn = (m: string) => warns.push(String(m))
  try {
    writeRaw(dataDir, '[]')
    assert.deepEqual([...loadAlwaysAllowed(dataDir)], [])
    writeRaw(dataDir, JSON.stringify({ alwaysAllow: 'nope' }))
    assert.deepEqual([...loadAlwaysAllowed(dataDir)], [])
    writeRaw(dataDir, JSON.stringify({ nope: 1 }))
    assert.deepEqual([...loadAlwaysAllowed(dataDir)], [])
  } finally {
    console.warn = orig
  }
  assert.equal(warns.length, 3, '三类非法形状各告警一次')
})

test('permission-memory: load 跳过非字符串/空白/超长/控制字符条目并 trim 合法项', (t) => {
  const dataDir = makeDataDir(t)
  writeRaw(
    dataDir,
    JSON.stringify({
      alwaysAllow: ['  terminal  ', '', '   ', 'x'.repeat(129), 'bad\u0001name', 42, null, { name: 'x' }, 'terminal']
    })
  )
  assert.deepEqual(listAlwaysAllowed(dataDir), ['terminal'])
})
