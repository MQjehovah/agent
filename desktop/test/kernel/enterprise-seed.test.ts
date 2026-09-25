import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { ENTERPRISE_FIELDS, seedEnterpriseConfig } from '../../electron/main/enterprise-seed'
import type { AppConfig } from '../../electron/main/store'

const BUNDLE = {
  oidcIssuer: 'https://auth.bundle.example',
  oidcClientId: 'dashboard-gateway',
  agentUrl: 'https://bundle.example/agent',
  ragUrl: 'https://bundle.example/rag',
  marketUrl: 'https://bundle.example/market',
  routerUrl: 'https://bundle.example/router',
  routerAdminUrl: 'https://bundle.example/router',
  updateFeedUrl: 'https://bundle.example/updates/dashboard',
  asrUrl: 'https://bundle.example/asr/v1'
}

function setup(opts: { stored?: Partial<AppConfig>; files?: Record<string, string> } = {}) {
  const files = opts.files ?? {}
  const updates: Array<Partial<AppConfig>> = []
  const logs: string[] = []
  const warns: string[] = []

  seedEnterpriseConfig({
    candidates: Object.keys(files).length > 0 ? Object.keys(files) : ['C:/pkg/enterprise.json'],
    stored: opts.stored ?? {},
    exists: (path) => Object.prototype.hasOwnProperty.call(files, path),
    readFile: (path) => files[path],
    update: (patch) => updates.push(patch),
    log: (message) => logs.push(message),
    warn: (message) => warns.push(message)
  })

  return { updates, logs, warns }
}

test('seed: 全新机器(stored 为空)从 enterprise.json 注入全部字段', () => {
  const { updates, logs } = setup({
    stored: {},
    files: { 'C:/pkg/enterprise.json': JSON.stringify(BUNDLE) }
  })
  assert.equal(updates.length, 1)
  assert.deepEqual(updates[0], BUNDLE)
  assert.equal(logs.length, 1)
  assert.match(logs[0], /已从 enterprise.json 注入/)
})

test('seed: 空 stored + 真实 enterprise.example.json → 模板字段全量注入,且不含 client_secret', () => {
  const file = fileURLToPath(new URL('../../build/enterprise.example.json', import.meta.url))
  const raw = readFileSync(file, 'utf8')
  const template = JSON.parse(raw) as Record<string, unknown>
  assert.equal(template.oidcClientSecret, undefined)

  const { updates } = setup({ stored: {}, files: { [file]: raw } })
  assert.equal(updates.length, 1)
  for (const key of ENTERPRISE_FIELDS) {
    const value = template[key]
    if (typeof value === 'string') {
      assert.equal(updates[0][key], value.trim(), key)
    }
  }
  assert.equal(updates[0].oidcClientSecret, undefined)
})

test('seed: stored 已有 issuer/secret → 不覆盖,仅补空字段', () => {
  const { updates } = setup({
    stored: {
      oidcIssuer: 'https://auth.user.example',
      oidcClientSecret: 'user-secret',
      updateFeedUrl: 'https://user.example/updates'
    },
    files: {
      'C:/pkg/enterprise.json': JSON.stringify({ ...BUNDLE, oidcClientSecret: 'bundle-secret' })
    }
  })
  assert.equal(updates.length, 1)
  assert.equal(updates[0].oidcIssuer, undefined)
  assert.equal(updates[0].oidcClientSecret, undefined)
  assert.equal(updates[0].updateFeedUrl, undefined)
  assert.equal(updates[0].oidcClientId, BUNDLE.oidcClientId)
  assert.equal(updates[0].agentUrl, BUNDLE.agentUrl)
  assert.equal(updates[0].routerAdminUrl, BUNDLE.routerAdminUrl)
})

test('seed: 白名单含 updateFeedUrl,老机器(已有其余字段)升级后补注入更新源', () => {
  assert.ok((ENTERPRISE_FIELDS as readonly string[]).includes('updateFeedUrl'))
  const legacy: Partial<AppConfig> = { ...BUNDLE }
  delete legacy.updateFeedUrl
  const { updates } = setup({
    stored: legacy,
    files: { 'C:/pkg/enterprise.json': JSON.stringify(BUNDLE) }
  })
  assert.equal(updates.length, 1)
  assert.equal(updates[0].updateFeedUrl, BUNDLE.updateFeedUrl)
  assert.equal(updates[0].oidcIssuer, undefined)
})

test('seed: 白名单含 asrUrl,老机器升级后补注入语音服务;未配置时跳过', () => {
  assert.ok((ENTERPRISE_FIELDS as readonly string[]).includes('asrUrl'))
  const legacy: Partial<AppConfig> = { ...BUNDLE }
  delete legacy.asrUrl
  const { updates } = setup({
    stored: legacy,
    files: { 'C:/pkg/enterprise.json': JSON.stringify(BUNDLE) }
  })
  assert.equal(updates.length, 1)
  assert.equal(updates[0].asrUrl, BUNDLE.asrUrl)
  assert.equal(updates[0].oidcIssuer, undefined)

  // 企业包内没配语音服务: 无补丁可写, 不落盘也不报错(语音输入保持禁用)
  const noAsr: Partial<AppConfig> = { ...BUNDLE }
  delete noAsr.asrUrl
  const bundleNoAsr: Partial<AppConfig> = { ...BUNDLE }
  delete bundleNoAsr.asrUrl
  const res = setup({ stored: noAsr, files: { 'C:/pkg/enterprise.json': JSON.stringify(bundleNoAsr) } })
  assert.equal(res.updates.length, 0)
})

test('seed: stored 已含全部必需字段(secret 可选)时早退,不读文件', () => {
  const { updates, warns } = setup({
    stored: { ...BUNDLE },
    files: { 'C:/pkg/enterprise.json': '{ 损坏的 JSON' }
  })
  assert.equal(updates.length, 0)
  assert.equal(warns.length, 0)
})

test('seed: enterprise.json 不存在 → 静默不注入', () => {
  const { updates, logs, warns } = setup({ stored: {} })
  assert.equal(updates.length, 0)
  assert.equal(logs.length, 0)
  assert.equal(warns.length, 0)
})

test('seed: 解析失败 → 警告并继续尝试下一候选文件', () => {
  const { updates, warns } = setup({
    stored: {},
    files: {
      'C:/pkg/broken.json': '{ 损坏的 JSON',
      'C:/pkg/enterprise.json': JSON.stringify(BUNDLE)
    }
  })
  assert.equal(warns.length, 1)
  assert.match(warns[0], /读取 C:\/pkg\/broken\.json 失败/)
  assert.deepEqual(updates, [BUNDLE])
})
