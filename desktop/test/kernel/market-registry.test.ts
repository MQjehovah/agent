import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync, mkdirSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { marketToolsFilePath, loadMarketToolsFile, saveMarketToolsFile } from '../../electron/main/kernel/market-registry'

function makeDataDir(t: { after(fn: () => void): void }, prefix = 'mtreg-'): string {
  const dir = mkdtempSync(join(tmpdir(), prefix))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  return dir
}

/** 直接写原始清单内容（自动建 localagent 目录），用于损坏/形状非法等容错场景 */
function writeRaw(dataDir: string, content: string): void {
  mkdirSync(join(dataDir, 'localagent'), { recursive: true })
  writeFileSync(marketToolsFilePath(dataDir), content)
}

test('market-registry: 文件缺失时 load 返回空', (t) => {
  const dataDir = makeDataDir(t)
  assert.deepEqual(loadMarketToolsFile(dataDir), [])
})

test('market-registry: save 后 load 往返一致（含 version/description，文件为 {tools:[...]}）', (t) => {
  const dataDir = makeDataDir(t)
  saveMarketToolsFile(dataDir, [
    { name: 'doc-check', version: '1.2.0', description: '检查文档规范' },
    { name: 'noop', version: '0.1.0' }
  ])
  const raw = readFileSync(marketToolsFilePath(dataDir), 'utf8')
  const parsed = JSON.parse(raw) as { tools?: unknown }
  assert.deepEqual(parsed.tools, [
    { name: 'doc-check', version: '1.2.0', description: '检查文档规范' },
    { name: 'noop', version: '0.1.0' }
  ])
  assert.deepEqual(loadMarketToolsFile(dataDir), [
    { name: 'doc-check', version: '1.2.0', description: '检查文档规范' },
    { name: 'noop', version: '0.1.0' }
  ])
})

test('market-registry: save 原子写不留 .tmp 残留', (t) => {
  const dataDir = makeDataDir(t)
  saveMarketToolsFile(dataDir, [{ name: 'a' }])
  saveMarketToolsFile(dataDir, [{ name: 'b' }])
  const entries = readdirSync(join(dataDir, 'localagent'))
  assert.deepEqual(entries, ['market-tools.json'], '多次覆盖后不应残留临时文件')
  assert.deepEqual(loadMarketToolsFile(dataDir), [{ name: 'b' }])
})

test('market-registry: 卸载语义=过滤后重存，重读不再出现该条且其余保留', (t) => {
  const dataDir = makeDataDir(t)
  saveMarketToolsFile(dataDir, [
    { name: 'keep', version: '1.0.0' },
    { name: 'drop', version: '2.0.0', description: '要卸载的工具' }
  ])
  // 复刻 ipc 卸载 tool 的读-滤-写三步
  const next = loadMarketToolsFile(dataDir).filter((r) => r.name !== 'drop')
  saveMarketToolsFile(dataDir, next)
  assert.deepEqual(loadMarketToolsFile(dataDir), [{ name: 'keep', version: '1.0.0' }])
  // 清单清空后写空数组仍是合法 JSON（覆盖写）
  saveMarketToolsFile(dataDir, [])
  assert.deepEqual(loadMarketToolsFile(dataDir), [])
})

test('market-registry: save 拒绝非法 name（防逃逸），且不落盘', (t) => {
  const dataDir = makeDataDir(t)
  for (const bad of ['../evil', 'a/b', 'bad?name', '..', '']) {
    assert.throws(() => saveMarketToolsFile(dataDir, [{ name: bad }]), /非法能力名/)
  }
  assert.ok(!existsSync(join(dataDir, 'localagent')), '非法名称不应产生任何文件')
})

test('market-registry: JSON 损坏时 load 返回空并 console.warn', (t) => {
  const dataDir = makeDataDir(t)
  writeRaw(dataDir, '{broken json')
  const orig = console.warn
  const warns: string[] = []
  console.warn = (m: string) => warns.push(String(m))
  try {
    assert.deepEqual(loadMarketToolsFile(dataDir), [])
  } finally {
    console.warn = orig
  }
  assert.ok(warns.length >= 1, '损坏时应告警一次')
  assert.ok(warns[0].includes('损坏'), warns[0])
})

test('market-registry: 顶层非对象 / tools 非数组时 load 返回空并告警', (t) => {
  const dataDir = makeDataDir(t)
  const orig = console.warn
  const warns: string[] = []
  console.warn = (m: string) => warns.push(String(m))
  try {
    writeRaw(dataDir, '[]')
    assert.deepEqual(loadMarketToolsFile(dataDir), [])
    writeRaw(dataDir, JSON.stringify({ tools: 'nope' }))
    assert.deepEqual(loadMarketToolsFile(dataDir), [])
    writeRaw(dataDir, JSON.stringify({ nope: 1 }))
    assert.deepEqual(loadMarketToolsFile(dataDir), [])
  } finally {
    console.warn = orig
  }
  assert.equal(warns.length, 3, '三类非法形状各告警一次')
})

test('market-registry: load 过滤缺 name/非法 name 的记录，保留合法项并 trim 字段', (t) => {
  const dataDir = makeDataDir(t)
  writeRaw(
    dataDir,
    JSON.stringify({
      tools: [
        { name: '  doc-check  ', version: ' 1.0.0 ', description: '  描述  ' },
        { name: '../evil' },
        { name: 'a/b' },
        { name: '' },
        { notName: true }
      ]
    })
  )
  assert.deepEqual(loadMarketToolsFile(dataDir), [
    { name: 'doc-check', version: '1.0.0', description: '描述' }
  ])
})

test('market-registry: 仅 name 的记录往返保持最小形状', (t) => {
  const dataDir = makeDataDir(t)
  saveMarketToolsFile(dataDir, [{ name: 'minimal' }])
  const parsed = JSON.parse(readFileSync(marketToolsFilePath(dataDir), 'utf8')) as { tools: unknown[] }
  assert.deepEqual(parsed.tools, [{ name: 'minimal' }])
  assert.deepEqual(loadMarketToolsFile(dataDir), [{ name: 'minimal' }])
})
