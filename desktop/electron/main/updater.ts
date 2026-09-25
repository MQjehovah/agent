/**
 * 自动更新:纯逻辑(状态文案 / 启用判定 / 事件绑定,依赖注入便于单测)+ 运行时装配。
 * electron-updater 只在 initAutoUpdater 运行时动态加载,单测环境不触碰 Electron。
 */

export type UpdateStatusEvent =
  | 'unconfigured'
  | 'idle'
  | 'checking'
  | 'available'
  | 'not-available'
  | 'downloaded'
  | 'error'

export interface UpdateInfoLike {
  /** 新版本号(available / downloaded 事件) */
  version?: string
  /** 错误详情(error 事件) */
  message?: string
}

/** 状态快照:主进程发给渲染层/系统通知的可读状态 */
export interface UpdateStatusSnapshot {
  event: UpdateStatusEvent
  /** UI 直接展示的状态文案 */
  message: string
  /** 已发现/已下载的版本号 */
  version?: string
}

export const UPDATE_FEED_UNCONFIGURED = '未配置更新源'

/** 更新源地址非法(不可解析/非 http(s)/无 host)时的状态文案 */
export const UPDATE_FEED_INVALID = '更新源地址非法'

/** 错误详情截断长度(含省略号),避免状态栏被长堆栈撑爆 */
export const UPDATE_ERROR_MAX = 120

/** 启动后延迟静默检查的默认毫秒数 */
export const UPDATE_CHECK_DELAY_MS = 30_000

/**
 * 规范化更新源地址:必须是可解析的 http/https 绝对地址且 host 非空。
 * 空串/纯空白或非法地址返回 null(调用方按「未配置」/「地址非法」区分提示)。
 */
export function normalizeUpdateFeedUrl(feedUrl: string): string | null {
  const trimmed = feedUrl.trim()
  if (!trimmed) return null
  try {
    const url = new URL(trimmed)
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
    if (!url.host) return null
    return trimmed
  } catch {
    return null
  }
}

function foldText(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
}

function truncate(text: string, max = UPDATE_ERROR_MAX): string {
  const flat = foldText(text)
  if (flat.length <= max) return flat
  return flat.slice(0, Math.max(0, max - 1)) + '…'
}

/** 事件 → 状态文案(纯函数,错误文案折叠并截断) */
export function formatUpdateStatus(event: UpdateStatusEvent, info?: UpdateInfoLike): string {
  const version = info?.version ?? ''
  switch (event) {
    case 'unconfigured':
      return info?.message?.trim() || UPDATE_FEED_UNCONFIGURED
    case 'idle':
      return '尚未检查更新'
    case 'checking':
      return '正在检查更新…'
    case 'available':
      return version ? `发现新版本 ${version}` : '发现新版本'
    case 'not-available':
      return '已是最新版本'
    case 'downloaded':
      return version ? `新版本 ${version} 已下载，点击重启安装` : '新版本已下载，点击重启安装'
    case 'error': {
      const detail = truncate(info?.message ?? '')
      return detail ? `检查更新失败:${detail}` : '检查更新失败'
    }
  }
}

function snapshot(event: UpdateStatusEvent, info?: UpdateInfoLike): UpdateStatusSnapshot {
  const message = formatUpdateStatus(event, info)
  return info?.version ? { event, message, version: info.version } : { event, message }
}

function toInfo(value: unknown): UpdateInfoLike {
  if (value && typeof value === 'object') {
    const record = value as { version?: unknown; message?: unknown }
    return {
      version: typeof record.version === 'string' ? record.version : undefined,
      message: typeof record.message === 'string' ? record.message : undefined
    }
  }
  return {}
}

/** 与 electron-updater autoUpdater 的能力子集对齐(便于注入假实现) */
export interface AutoUpdaterLike {
  autoDownload: boolean
  autoInstallOnAppQuit: boolean
  setFeedURL(options: { provider: 'generic'; url: string }): void
  checkForUpdates(): Promise<unknown> | unknown
  quitAndInstall(): void
  on(event: string, listener: (...args: unknown[]) => void): unknown
  removeAllListeners(event?: string): unknown
}

export interface BindAutoUpdaterOptions {
  feedUrl: string
  autoCheck: boolean
  /** 下载完成后提示(主进程接系统通知) */
  notify(options: { title: string; body: string }): void
  /** 每次状态变化回调(主进程转发渲染层) */
  onStatus(snapshot: UpdateStatusSnapshot): void
  /** 启动后延迟静默检查毫秒数,默认 30s */
  delayMs?: number
  setTimer?(callback: () => void, ms: number): unknown
  warn?(message: string): void
}

export interface UpdaterHandle {
  /** 是否启用(配置了更新源) */
  enabled: boolean
  status(): UpdateStatusSnapshot
  /** 手动检查,返回可读状态快照(不抛错) */
  checkForUpdatesNow(): Promise<UpdateStatusSnapshot>
  /** 仅在已下载完成时真正退出安装,避免误退出 */
  quitAndInstall(): { ok: boolean; error?: string }
}

/**
 * 绑定 autoUpdater(依赖注入):未配置源 → 不启用且手动检查返回「未配置更新源」;
 * 已配置 → generic provider、autoDownload、手动检查 + 可选延迟静默检查。
 */
export function bindAutoUpdater(updater: AutoUpdaterLike, options: BindAutoUpdaterOptions): UpdaterHandle {
  const rawFeedUrl = options.feedUrl.trim()
  const feedUrl = normalizeUpdateFeedUrl(rawFeedUrl)
  let current = snapshot('unconfigured')

  const publish = (event: UpdateStatusEvent, info?: UpdateInfoLike): UpdateStatusSnapshot => {
    current = snapshot(event, info)
    options.onStatus(current)
    return current
  }

  if (!feedUrl) {
    // 未配置更新源 / 地址非法:不触碰 autoUpdater(不 setFeedURL、不排检查),手动检查返回可读状态
    const message = rawFeedUrl ? UPDATE_FEED_INVALID : UPDATE_FEED_UNCONFIGURED
    if (rawFeedUrl) options.warn?.(`[update] ${UPDATE_FEED_INVALID}: ${rawFeedUrl}`)
    publish('unconfigured', { message })
    return {
      enabled: false,
      status: () => current,
      checkForUpdatesNow: async () => current,
      quitAndInstall: () => ({ ok: false, error: message })
    }
  }

  // 重复初始化(配置变化/重新绑定)时清掉旧监听,避免状态重复回推
  updater.removeAllListeners()
  updater.autoDownload = true
  // 安装只经 quitAndInstall(「点击重启安装」),禁止退出时静默安装
  updater.autoInstallOnAppQuit = false
  updater.setFeedURL({ provider: 'generic', url: feedUrl })
  publish('idle')

  updater.on('checking-for-update', () => publish('checking'))
  updater.on('update-available', (...args) => publish('available', toInfo(args[0])))
  updater.on('update-not-available', () => publish('not-available'))
  updater.on('update-downloaded', (...args) => {
    const status = publish('downloaded', toInfo(args[0]))
    // 通知点击由主进程接管(重启安装),文案已含「点击重启安装」
    options.notify({ title: 'Dashboard 更新', body: status.message })
  })
  updater.on('error', (...args) => {
    const err = args[0]
    publish('error', { message: err instanceof Error ? err.message : String(err ?? '') })
  })

  async function runCheck(): Promise<UpdateStatusSnapshot> {
    try {
      const result = await updater.checkForUpdates()
      if (result === null || result === undefined) {
        // 未打包(开发模式)时 electron-updater 直接跳过,给出可读终态避免卡在「检查中」
        options.warn?.('[update] 当前环境未启用更新检查(未打包)')
        publish('not-available')
      }
    } catch (err) {
      publish('error', { message: (err as Error)?.message ?? String(err) })
    }
    return current
  }

  if (options.autoCheck) {
    const delayMs = Math.max(0, options.delayMs ?? UPDATE_CHECK_DELAY_MS)
    const setTimer = options.setTimer ?? ((callback: () => void, ms: number): unknown => setTimeout(callback, ms))
    // 静默检查:失败只体现在状态里,不打断使用
    setTimer(() => {
      void runCheck()
    }, delayMs)
  }

  return {
    enabled: true,
    status: () => current,
    checkForUpdatesNow: runCheck,
    quitAndInstall: () => {
      if (current.event !== 'downloaded') return { ok: false, error: '更新尚未下载完成' }
      updater.quitAndInstall()
      return { ok: true }
    }
  }
}

export interface InitAutoUpdaterOptions {
  feedUrl: string
  autoCheck: boolean
  notify(options: { title: string; body: string }): void
  onStatus(snapshot: UpdateStatusSnapshot): void
}

let boundHandle: UpdaterHandle | null = null
let lastStatus: UpdateStatusSnapshot = snapshot('unconfigured')

/**
 * 运行时装配:动态加载 electron-updater(单测/未安装时不加载),
 * feedUrl 为空 → 「未配置更新源」,非法地址 → 「更新源地址非法」,均不加载、不发起检查。
 */
export function initAutoUpdater(options: InitAutoUpdaterOptions): void {
  const publish = (status: UpdateStatusSnapshot): void => {
    lastStatus = status
    options.onStatus(status)
  }

  const rawFeedUrl = options.feedUrl.trim()
  if (!rawFeedUrl) {
    boundHandle = null
    publish(snapshot('unconfigured'))
    return
  }
  const feedUrl = normalizeUpdateFeedUrl(rawFeedUrl)
  if (!feedUrl) {
    boundHandle = null
    console.warn(`[update] ${UPDATE_FEED_INVALID}: ${rawFeedUrl}`)
    publish(snapshot('unconfigured', { message: UPDATE_FEED_INVALID }))
    return
  }

  void import('electron-updater')
    .then((module) => {
      const autoUpdater = (module as unknown as { autoUpdater: AutoUpdaterLike }).autoUpdater
      boundHandle = bindAutoUpdater(autoUpdater, {
        feedUrl,
        autoCheck: options.autoCheck,
        notify: options.notify,
        onStatus: publish
      })
    })
    .catch((err) => {
      publish(snapshot('error', { message: (err as Error)?.message ?? String(err) }))
    })
}

/** 当前更新状态快照(app:update:status 与手动检查共用) */
export function getUpdateStatus(): UpdateStatusSnapshot {
  return boundHandle?.status() ?? lastStatus
}

/** 手动检查更新:未配置/尚未装配时返回当前可读状态,不抛错 */
export async function checkForUpdatesNow(): Promise<UpdateStatusSnapshot> {
  if (!boundHandle) return lastStatus
  return boundHandle.checkForUpdatesNow()
}

/** 退出并安装已下载的更新(未下载完成时拒绝) */
export function quitAndInstallUpdate(): { ok: boolean; error?: string } {
  if (!boundHandle) return { ok: false, error: '更新模块未启用' }
  return boundHandle.quitAndInstall()
}
