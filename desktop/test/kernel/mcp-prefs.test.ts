import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { mcpPrefsFilePath, loadDisabled, saveDisabled, setDisabled } from '../../electron/main/kernel/mcp-prefs'

function makeDataDir(t: { after(fn: () => void): void }, prefix = 'mcpprefs-'): string {
  const dir = mkdtempSync(join(tmpdir(), prefix))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  return dir
}

/** 直接写原始偏好内容（自动建 localagent 目录），用于损坏/形状非法等容错场景 */
function writeRaw(dataDir: string, content: string): void {
  mkdirSync(join(dataDir, 'localagent'), { recursive: true })
  writeFileSync(mcpPrefsFilePath(dataDir), content)
}

test('mcp-prefs: 文件缺失时 load 返回空集合（缺省=启用）', (t) => {
  const dataDir = makeDataDir(t)
  assert.deepEqual([...loadDisabled(dataDir)], [])
})

test('mcp-prefs: setDisabled 禁用后 load 含该 name，再次启用后移除（读写往返）', (t) => {
  const dataDir = makeDataDir(t)
  setDisabled(dataDir, 'time', true)
  assert.deepEqual([...loadDisabled(dataDir)], ['time'])
  setDisabled(dataDir, 'filesystem', true)
  assert.deepEqual([...loadDisabled(dataDir)].sort(), ['filesystem', 'time'])
  setDisabled(dataDir, 'time', false)
  assert.deepEqual([...loadDisabled(dataDir)], ['filesystem'])
})

test('mcp-prefs: 落盘形状为 { disabled: [...] }（排序去重、JSON 可读）', (t) => {
  const dataDir = makeDataDir(t)
  setDisabled(dataDir, 'zeta', true)
  setDisabled(dataDir, 'alpha', true)
  const raw = readFileSync(mcpPrefsFilePath(dataDir), 'utf8')
  assert.deepEqual(JSON.parse(raw) as unknown, { disabled: ['alpha', 'zeta'] })
})

test('mcp-prefs: 重复 set 同一 name 幂等且文件内不重复', (t) => {
  const dataDir = makeDataDir(t)
  setDisabled(dataDir, 'time', true)
  setDisabled(dataDir, 'time', true)
  setDisabled(dataDir, 'time', true)
  const parsed = JSON.parse(readFileSync(mcpPrefsFilePath(dataDir), 'utf8')) as { disabled: string[] }
  assert.deepEqual(parsed.disabled, ['time'])
  assert.deepEqual([...loadDisabled(dataDir)], ['time'])
})

test('mcp-prefs: setDisabled(false) 对未禁用项幂等（落盘 disabled: []），原子写不留 .tmp', (t) => {
  const dataDir = makeDataDir(t)
  setDisabled(dataDir, 'time', false)
  const entries = readdirSync(join(dataDir, 'localagent'))
  assert.deepEqual(entries, ['mcp-prefs.json'], '多次写入不应残留临时文件')
  assert.deepEqual([...loadDisabled(dataDir)], [])
})

test('mcp-prefs: JSON 损坏时 load 返回空并 console.warn', (t) => {
  const dataDir = makeDataDir(t)
  writeRaw(dataDir, '{broken json')
  const orig = console.warn
  const warns: string[] = []
  console.warn = (m: string) => warns.push(String(m))
  try {
    assert.deepEqual([...loadDisabled(dataDir)], [])
  } finally {
    console.warn = orig
  }
  assert.ok(warns.length >= 1, '损坏时应告警一次')
  assert.ok(warns[0].includes('损坏'), warns[0])
})

test('mcp-prefs: 顶层非对象 / disabled 非数组时 load 返回空并告警', (t) => {
  const dataDir = makeDataDir(t)
  const orig = console.warn
  const warns: string[] = []
  console.warn = (m: string) => warns.push(String(m))
  try {
    writeRaw(dataDir, '[]')
    assert.deepEqual([...loadDisabled(dataDir)], [])
    writeRaw(dataDir, JSON.stringify({ disabled: 'nope' }))
    assert.deepEqual([...loadDisabled(dataDir)], [])
    writeRaw(dataDir, JSON.stringify({ nope: 1 }))
    assert.deepEqual([...loadDisabled(dataDir)], [])
  } finally {
    console.warn = orig
  }
  assert.equal(warns.length, 3, '三类非法形状各告警一次')
})

test('mcp-prefs: load 跳过非字符串/非法/空白 name 条目并 trim 合法项', (t) => {
  const dataDir = makeDataDir(t)
  writeRaw(
    dataDir,
    JSON.stringify({
      disabled: ['  time  ', '../evil', 'a/b', '', 42, { name: 'x' }, 'time']
    })
  )
  assert.deepEqual([...loadDisabled(dataDir)], ['time'])
})

test('mcp-prefs: setDisabled 拒绝非法 name（防逃逸），且不落盘', (t) => {
  const dataDir = makeDataDir(t)
  for (const bad of ['../evil', 'a/b', 'bad?name', '..', '']) {
    assert.throws(() => setDisabled(dataDir, bad, true), /非法能力名/)
    assert.throws(() => saveDisabled(dataDir, [bad]), /非法能力名/)
  }
  assert.ok(!existsSync(join(dataDir, 'localagent')), '非法名称不应产生任何文件')
})
