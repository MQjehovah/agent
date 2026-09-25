import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  buildDefaultConfig,
  createConfigStore,
  filterUserConfigPatch,
  USER_CONFIG_KEYS,
  type AppConfig
} from '../../electron/main/config-core'

const CTX = { home: 'C:/Users/tester', env: {} }

function memoryStore(defaults: AppConfig, initial?: string) {
  let text: string | null = initial ?? null
  const writes: string[] = []
  const store = createConfigStore(defaults, {
    readText: () => text,
    writeText: (next) => {
      text = next
      writes.push(next)
    }
  })
  return { store, writes, text: () => text }
}

test('desktop-config：新增桌面设置项默认值符合设计要求', () => {
  const cfg = buildDefaultConfig(CTX)
  assert.equal(cfg.closeToTray, true)
  assert.equal(cfg.quickHotkey, 'Alt+Space')
  assert.equal(cfg.launchAtLogin, false)
  assert.equal(cfg.notifyOnFinish, true)
  assert.equal(cfg.notifySound, false)
})

test('desktop-config：自动更新默认开启,更新源为空由企业注入', () => {
  const cfg = buildDefaultConfig(CTX)
  assert.equal(cfg.autoCheckUpdate, true)
  assert.equal(cfg.updateFeedUrl, '')
})

test('desktop-config：本地 agent 渐进披露默认 auto, 渲染层可写且在用户偏好白名单内', () => {
  const cfg = buildDefaultConfig(CTX)
  assert.equal(cfg.localAgentToolSearch, 'auto')
  assert.ok((USER_CONFIG_KEYS as readonly string[]).includes('localAgentToolSearch'))
})

test('desktop-config：localAgentToolSearch 白名单接受三档合法值', () => {
  for (const mode of ['auto', 'always', 'off'] as const) {
    const { patch, ignored } = filterUserConfigPatch({ localAgentToolSearch: mode })
    assert.deepEqual(patch, { localAgentToolSearch: mode }, mode)
    assert.deepEqual(ignored, [], mode)
  }
})

test('desktop-config：localAgentToolSearch 非法值拒绝写入且记录(不回退静默落盘)', () => {
  for (const bad of ['banana', '', 1, true, null, {}, ['always']]) {
    const { patch, ignored } = filterUserConfigPatch({ localAgentToolSearch: bad } as unknown as Partial<AppConfig>)
    assert.deepEqual(patch, {}, String(bad))
    assert.deepEqual(ignored, ['localAgentToolSearch'], String(bad))
  }
  // undefined 同样拒绝(不覆盖既有值)
  const { patch, ignored } = filterUserConfigPatch({ localAgentToolSearch: undefined })
  assert.deepEqual(patch, {})
  assert.deepEqual(ignored, ['localAgentToolSearch'])
})

test('desktop-config：ASR 地址默认为空(由企业注入),渲染层不可写', () => {
  const cfg = buildDefaultConfig(CTX)
  assert.equal(cfg.asrUrl, '')
  assert.ok(!(USER_CONFIG_KEYS as readonly string[]).includes('asrUrl'))
  const { patch, ignored } = filterUserConfigPatch({ asrUrl: 'https://evil.example/v1' } as Partial<AppConfig>)
  assert.ok(!('asrUrl' in patch))
  assert.deepEqual(ignored, ['asrUrl'])
})

test('desktop-config：自动更新配置写入后可读回(roundtrip)', () => {
  const { store } = memoryStore(buildDefaultConfig(CTX))
  assert.equal(store.get().autoCheckUpdate, true)

  store.update({ autoCheckUpdate: false, updateFeedUrl: 'https://ai.xzrobot.com/updates/dashboard' })

  assert.equal(store.get().autoCheckUpdate, false)
  assert.equal(store.get().updateFeedUrl, 'https://ai.xzrobot.com/updates/dashboard')
})

test('desktop-config：config:set 键白名单只放行用户偏好键,企业/敏感字段忽略并记录', () => {
  const input = {
    theme: 'light',
    closeToTray: false,
    permissionMode: 'auto',
    updateFeedUrl: 'https://evil.example/updates',
    asrUrl: 'https://evil.example/asr',
    oidcClientSecret: 'stolen-secret',
    oidcIssuer: 'https://evil.example',
    agentUrl: 'https://evil.example/agent',
    routerAdminUrl: 'https://evil.example/router',
    agentServiceToken: 'stolen-token',
    unknownField: 1
  } as Partial<AppConfig>
  const { patch, ignored } = filterUserConfigPatch(input)

  assert.deepEqual(patch, { theme: 'light', closeToTray: false, permissionMode: 'auto' })
  assert.ok(!('updateFeedUrl' in patch))
  assert.deepEqual(ignored.sort(), [
    'agentServiceToken',
    'agentUrl',
    'asrUrl',
    'oidcClientSecret',
    'oidcIssuer',
    'routerAdminUrl',
    'unknownField',
    'updateFeedUrl'
  ])
})

test('desktop-config：白名单覆盖全部用户偏好键,空补丁安全', () => {
  const all = {
    theme: 'light',
    localModel: 'm',
    defaultWorkspace: 'C:/ws',
    permissionMode: 'auto',
    localAgentToolSearch: 'always',
    closeToTray: false,
    quickHotkey: 'F2',
    launchAtLogin: true,
    notifyOnFinish: false,
    notifySound: true,
    autoCheckUpdate: false
  } as Partial<AppConfig>
  const { patch, ignored } = filterUserConfigPatch(all)
  assert.deepEqual(Object.keys(patch).sort(), [...USER_CONFIG_KEYS].sort())
  assert.deepEqual(ignored, [])

  assert.deepEqual(filterUserConfigPatch(undefined), { patch: {}, ignored: [] })
  assert.deepEqual(filterUserConfigPatch(null), { patch: {}, ignored: [] })
})

test('desktop-config：主进程内部 updateConfig 直写企业字段不受渲染层白名单影响', () => {
  const { store } = memoryStore(buildDefaultConfig(CTX))
  store.update({ updateFeedUrl: 'https://ai.xzrobot.com/updates/dashboard', oidcClientSecret: 'seed-secret' })
  assert.equal(store.get().updateFeedUrl, 'https://ai.xzrobot.com/updates/dashboard')
  assert.equal(store.get().oidcClientSecret, 'seed-secret')
})

test('desktop-config：env 注入优先,home 生成默认工作区', () => {
  const cfg = buildDefaultConfig({
    home: 'C:/Users/tester',
    env: { OIDC_ISSUER: 'http://env:8091', AGENT_SERVICE_TOKEN: 'tok' }
  })
  assert.equal(cfg.oidcIssuer, 'http://env:8091')
  assert.equal(cfg.agentServiceToken, 'tok')
  assert.ok(cfg.defaultWorkspace.includes('local-workspace'))
})

test('desktop-config：新配置项写入后可读回(roundtrip),未写字段保持默认', () => {
  const { store } = memoryStore(buildDefaultConfig(CTX))
  assert.equal(store.get().quickHotkey, 'Alt+Space')

  const next = store.update({ quickHotkey: 'Ctrl+Shift+A', notifySound: true, closeToTray: false })

  assert.equal(next.quickHotkey, 'Ctrl+Shift+A')
  assert.equal(next.notifySound, true)
  assert.equal(next.closeToTray, false)
  assert.equal(store.get().quickHotkey, 'Ctrl+Shift+A')
  assert.equal(store.get().notifySound, true)
  assert.equal(store.get().closeToTray, false)
  // 未写入的字段保持默认
  assert.equal(store.get().launchAtLogin, false)
  assert.equal(store.get().notifyOnFinish, true)
  assert.equal(store.get().theme, 'dark')
})

test('desktop-config：update 落盘内容为合并后的完整配置', () => {
  const { store, text } = memoryStore(buildDefaultConfig(CTX))
  store.update({ notifyOnFinish: false })
  const written = JSON.parse(text() ?? '{}') as Record<string, unknown>
  assert.equal(written.notifyOnFinish, false)
  assert.equal(written.quickHotkey, 'Alt+Space')
  assert.equal(written.agentUrl, 'https://ai.xzrobot.com/agent')
})

test('desktop-config：启动时读回磁盘上的桌面设置项', () => {
  const initial = JSON.stringify({ quickHotkey: 'Ctrl+Alt+Q', launchAtLogin: true, closeToTray: false })
  const { store } = memoryStore(buildDefaultConfig(CTX), initial)
  const cfg = store.get()
  assert.equal(cfg.quickHotkey, 'Ctrl+Alt+Q')
  assert.equal(cfg.launchAtLogin, true)
  assert.equal(cfg.closeToTray, false)
  assert.equal(cfg.notifySound, false)
})

test('desktop-config：stored 只返回原始存储内容,不合并默认值', () => {
  const { store } = memoryStore(buildDefaultConfig(CTX), JSON.stringify({ quickHotkey: 'F2' }))
  assert.deepEqual(store.stored(), { quickHotkey: 'F2' })
})

test('desktop-config：文件缺失或 JSON 损坏时回退默认值,不抛错', () => {
  const missing = memoryStore(buildDefaultConfig(CTX))
  assert.equal(missing.store.get().quickHotkey, 'Alt+Space')
  assert.deepEqual(missing.store.stored(), {})

  const broken = memoryStore(buildDefaultConfig(CTX), '{ 损坏的 JSON')
  assert.equal(broken.store.get().notifyOnFinish, true)
  assert.deepEqual(broken.store.stored(), {})
})
