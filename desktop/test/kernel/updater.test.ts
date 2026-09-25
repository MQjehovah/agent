import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  bindAutoUpdater,
  formatUpdateStatus,
  normalizeUpdateFeedUrl,
  UPDATE_ERROR_MAX,
  type AutoUpdaterLike,
  type UpdateStatusSnapshot
} from '../../electron/main/updater'

function fakeUpdater(checkImpl?: () => Promise<unknown> | unknown) {
  const listeners = new Map<string, Array<(...args: unknown[]) => void>>()
  const calls = {
    feedUrls: [] as Array<{ provider: string; url: string }>,
    checks: 0,
    installs: 0
  }
  const updater: AutoUpdaterLike = {
    autoDownload: false,
    // electron-updater 默认 true;绑定时必须显式关掉,防止退出时静默安装
    autoInstallOnAppQuit: true,
    setFeedURL: (options) => {
      calls.feedUrls.push(options)
    },
    checkForUpdates: () => {
      calls.checks += 1
      return checkImpl ? checkImpl() : Promise.resolve({ updateInfo: { version: '0.1.0' } })
    },
    quitAndInstall: () => {
      calls.installs += 1
    },
    on: (event, listener) => {
      const list = listeners.get(event) ?? []
      list.push(listener)
      listeners.set(event, list)
      return updater
    },
    removeAllListeners: () => {
      listeners.clear()
    }
  }
  return {
    updater,
    calls,
    emit: (event: string, ...args: unknown[]): void => {
      for (const listener of listeners.get(event) ?? []) listener(...args)
    },
    listenerCount: (event: string): number => listeners.get(event)?.length ?? 0
  }
}

function setup(opts: { feedUrl?: string; autoCheck?: boolean; checkImpl?: () => Promise<unknown> | unknown } = {}) {
  const fake = fakeUpdater(opts.checkImpl)
  const statuses: UpdateStatusSnapshot[] = []
  const notifications: Array<{ title: string; body: string }> = []
  const timers: Array<() => void> = []
  const warns: string[] = []
  const handle = bindAutoUpdater(fake.updater, {
    feedUrl: opts.feedUrl ?? 'https://ai.xzrobot.com/updates/dashboard',
    autoCheck: opts.autoCheck ?? false,
    notify: (payload) => notifications.push(payload),
    onStatus: (status) => statuses.push(status),
    delayMs: 30_000,
    setTimer: (callback) => {
      timers.push(callback)
      return 0
    },
    warn: (message) => warns.push(message)
  })
  return {
    ...fake,
    handle,
    statuses,
    notifications,
    timers,
    warns,
    last: (): UpdateStatusSnapshot => statuses[statuses.length - 1]
  }
}

test('updater: formatUpdateStatus 各事件文案映射', () => {
  assert.equal(formatUpdateStatus('unconfigured'), '未配置更新源')
  assert.equal(formatUpdateStatus('unconfigured', { message: '更新源地址非法' }), '更新源地址非法')
  assert.equal(formatUpdateStatus('idle'), '尚未检查更新')
  assert.equal(formatUpdateStatus('checking'), '正在检查更新…')
  assert.equal(formatUpdateStatus('available', { version: '0.2.0' }), '发现新版本 0.2.0')
  assert.equal(formatUpdateStatus('available'), '发现新版本')
  assert.equal(formatUpdateStatus('not-available'), '已是最新版本')
  assert.equal(formatUpdateStatus('downloaded', { version: '0.2.0' }), '新版本 0.2.0 已下载，点击重启安装')
  assert.equal(formatUpdateStatus('downloaded'), '新版本已下载，点击重启安装')
  assert.equal(formatUpdateStatus('error', { message: 'net::ERR_CONNECTION_REFUSED' }), '检查更新失败:net::ERR_CONNECTION_REFUSED')
  assert.equal(formatUpdateStatus('error'), '检查更新失败')
})

test('updater: 错误文案折叠空白并截断到上限', () => {
  const noise = `  ${'x'.repeat(400)}\n  tail  `
  const text = formatUpdateStatus('error', { message: noise })
  assert.ok(text.startsWith('检查更新失败:'))
  assert.ok(text.endsWith('…'))
  assert.ok(text.length <= '检查更新失败:'.length + UPDATE_ERROR_MAX)
  assert.ok(!text.includes('\n'))
})

test('updater: updateFeedUrl 为空/纯空白 → 不启用,不设置源,不排检查', async () => {
  for (const feedUrl of ['', '   ']) {
    const ctx = setup({ feedUrl, autoCheck: true })
    assert.equal(ctx.handle.enabled, false)
    assert.equal(ctx.calls.feedUrls.length, 0)
    assert.equal(ctx.timers.length, 0)
    assert.equal(ctx.last().event, 'unconfigured')
    assert.equal(ctx.last().message, '未配置更新源')
    const status = await ctx.handle.checkForUpdatesNow()
    assert.equal(status.event, 'unconfigured')
    assert.equal(ctx.calls.checks, 0)
    assert.deepEqual(ctx.handle.quitAndInstall(), { ok: false, error: '未配置更新源' })
  }
})

test('updater: normalizeUpdateFeedUrl 只接受可解析的 http/https 绝对地址', () => {
  assert.equal(normalizeUpdateFeedUrl('https://ai.xzrobot.com/updates/dashboard'), 'https://ai.xzrobot.com/updates/dashboard')
  assert.equal(normalizeUpdateFeedUrl('  http://10.0.0.8:8080/updates  '), 'http://10.0.0.8:8080/updates')
  assert.equal(normalizeUpdateFeedUrl(''), null)
  assert.equal(normalizeUpdateFeedUrl('   '), null)
  assert.equal(normalizeUpdateFeedUrl('ftp://ai.xzrobot.com/updates'), null)
  assert.equal(normalizeUpdateFeedUrl('file:///C:/updates'), null)
  assert.equal(normalizeUpdateFeedUrl('javascript:alert(1)'), null)
  assert.equal(normalizeUpdateFeedUrl('updates/dashboard'), null)
  assert.equal(normalizeUpdateFeedUrl('///'), null)
})

test('updater: 更新源地址非法 → 不启用、不设置源、不排检查、可读状态', async () => {
  for (const feedUrl of ['ftp://ai.xzrobot.com/updates', 'not a url', 'javascript:alert(1)']) {
    const ctx = setup({ feedUrl, autoCheck: true })
    assert.equal(ctx.handle.enabled, false)
    assert.equal(ctx.calls.feedUrls.length, 0, feedUrl)
    assert.equal(ctx.timers.length, 0, feedUrl)
    assert.equal(ctx.last().event, 'unconfigured')
    assert.equal(ctx.last().message, '更新源地址非法')
    assert.equal(ctx.warns.length, 1)
    const status = await ctx.handle.checkForUpdatesNow()
    assert.equal(status.event, 'unconfigured')
    assert.equal(ctx.calls.checks, 0)
    assert.deepEqual(ctx.handle.quitAndInstall(), { ok: false, error: '更新源地址非法' })
  }
})

test('updater: updateFeedUrl 有值 → generic 源 + autoDownload,autoCheck 延迟静默检查', async () => {
  const ctx = setup({ feedUrl: '  https://ai.xzrobot.com/updates/dashboard  ', autoCheck: true })
  assert.equal(ctx.handle.enabled, true)
  assert.deepEqual(ctx.calls.feedUrls, [{ provider: 'generic', url: 'https://ai.xzrobot.com/updates/dashboard' }])
  assert.equal(ctx.updater.autoDownload, true)
  // 禁止退出时静默安装:安装只经 quitAndInstall(「点击重启安装」)
  assert.equal(ctx.updater.autoInstallOnAppQuit, false)
  assert.equal(ctx.last().event, 'idle')
  // 延迟检查:定时器未触发前不检查,触发后只检查一次
  assert.equal(ctx.timers.length, 1)
  assert.equal(ctx.calls.checks, 0)
  ctx.timers[0]()
  await Promise.resolve()
  assert.equal(ctx.calls.checks, 1)
  // 手动检查仍可用(假实现不发事件,状态保持 idle 由事件驱动)
  const status = await ctx.handle.checkForUpdatesNow()
  assert.equal(ctx.calls.checks, 2)
  assert.equal(status.event, 'idle')
})

test('updater: autoCheck=false → 不排静默检查,仅手动可用', async () => {
  const ctx = setup({ autoCheck: false })
  assert.equal(ctx.timers.length, 0)
  await ctx.handle.checkForUpdatesNow()
  assert.equal(ctx.calls.checks, 1)
})

test('updater: 事件驱动状态,下载完成发通知并允许重启安装', () => {
  const ctx = setup()
  ctx.emit('checking-for-update')
  assert.equal(ctx.last().event, 'checking')
  ctx.emit('update-available', { version: '0.2.0' })
  assert.equal(ctx.last().event, 'available')
  assert.match(ctx.last().message, /0\.2\.0/)
  // 未下载完成不允许安装
  assert.equal(ctx.handle.quitAndInstall().ok, false)
  assert.equal(ctx.calls.installs, 0)
  ctx.emit('update-not-available')
  assert.equal(ctx.last().event, 'not-available')
  ctx.emit('update-downloaded', { version: '0.2.0' })
  assert.equal(ctx.last().event, 'downloaded')
  assert.equal(ctx.notifications.length, 1)
  assert.match(ctx.notifications[0].body, /0\.2\.0/)
  assert.match(ctx.notifications[0].body, /重启安装/)
  assert.deepEqual(ctx.handle.quitAndInstall(), { ok: true })
  assert.equal(ctx.calls.installs, 1)
})

test('updater: error 事件降级为可读状态,不抛错', () => {
  const ctx = setup()
  ctx.emit('error', new Error('net::ERR_CONNECTION_REFUSED'))
  assert.equal(ctx.last().event, 'error')
  assert.equal(ctx.last().message, '检查更新失败:net::ERR_CONNECTION_REFUSED')
  // 非 Error 值也要能兜底
  ctx.emit('error', 'boom')
  assert.equal(ctx.last().message, '检查更新失败:boom')
})

test('updater: 检查被拒绝/开发模式跳过 → not-available,不卡在检查中', async () => {
  const skipped = setup({ checkImpl: () => null })
  const status = await skipped.handle.checkForUpdatesNow()
  assert.equal(status.event, 'not-available')
  assert.equal(skipped.warns.length, 1)

  const failed = setup({
    checkImpl: () => {
      throw new Error('EAI_AGAIN')
    }
  })
  const errStatus = await failed.handle.checkForUpdatesNow()
  assert.equal(errStatus.event, 'error')
  assert.equal(errStatus.message, '检查更新失败:EAI_AGAIN')
})

test('updater: 重复绑定清空旧监听,不重复回推状态', () => {
  const fake = fakeUpdater()
  const options = {
    feedUrl: 'https://ai.xzrobot.com/updates/dashboard',
    autoCheck: false,
    notify: () => {},
    onStatus: () => {}
  }
  bindAutoUpdater(fake.updater, options)
  bindAutoUpdater(fake.updater, options)
  assert.equal(fake.listenerCount('checking-for-update'), 1)
  assert.equal(fake.listenerCount('update-downloaded'), 1)
})
