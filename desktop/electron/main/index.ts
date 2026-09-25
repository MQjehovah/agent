import { app, BrowserWindow, desktopCapturer, ipcMain, nativeImage, Notification, screen, shell } from 'electron'
import { join } from 'node:path'
import { getConfig, updateConfig, getStoredConfig, type AppConfig } from './store'
import { filterUserConfigPatch } from './config-core'
import { existsSync, readFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { registerUpstreamIpc } from './upstream'
import { registerKernelIpc } from './kernel/ipc'
import { registerMediaProtocol, registerMediaScheme } from './media'
import { startSsoLogin, getIdentity, clearIdentity, restoreIdentity, ensureRouterKey } from './identity'
import { getUsageSummary } from './usage'
import { seedEnterpriseConfig as runEnterpriseSeed } from './enterprise-seed'
import { setupTray, destroyTray, hasTray, showFirstHideBalloon, resolveTrayIconPath } from './tray'
import { closeQuickWindow, getHotkeyStatus, registerQuickHotkey, sendToQuickWindow } from './quick'
import { handleFinishNotification, type FinishNotificationPayload } from './notify'
import { checkForUpdatesNow, getUpdateStatus, initAutoUpdater, quitAndInstallUpdate } from './updater'
import { pickScreenSource, screenshotFailureText } from './screenshot'
import { transcribeAudio, type AsrResult } from './asr'

let mainWindow: BrowserWindow | null = null
/** 真正退出(托盘退出/系统退出)前,关闭主窗不拦截 */
let isQuitting = false

/** 标题栏叠条配色:深色近黑/浅字,浅色近白/深字 */
function overlayColors(theme: 'dark' | 'light'): { color: string; symbolColor: string } {
  return theme === 'light'
    ? { color: '#f5f7fa', symbolColor: '#3c3c3c' }
    : { color: '#141414', symbolColor: '#d0d0d0' }
}

/** 运行时刷新原生最小化/最大化/关闭按钮配色(仅 Windows WCO 支持,失败静默) */
function applyTitleBarOverlay(theme: 'dark' | 'light'): void {
  try {
    mainWindow?.setTitleBarOverlay(overlayColors(theme))
  } catch {
    // 非 Windows 或 WCO 未启用时无此能力
  }
}

/** 显示并聚焦主窗口(最小化时先还原);窗口不存在则重建 */
function showMainWindow(): void {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createWindow()
    return
  }
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.show()
  mainWindow.focus()
}

/** 托盘左键:窗口已显示则收起,否则显示聚焦(点击托盘会使窗口失焦,不能用 isFocused 判定) */
function toggleMainWindow(): void {
  if (mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible() && !mainWindow.isMinimized()) {
    mainWindow.hide()
    return
  }
  showMainWindow()
}

/**
 * 主窗口渲染层是否已挂载并注册好事件监听(由渲染层 desktop:ready 确认)。
 * did-finish-load 时 WorkbenchLayout 尚未挂载,此时发送仍会丢事件,故必须等这个信号。
 */
let mainWindowReady = false
/** 渲染层未就绪期间暂存的主进程事件,就绪后按序补发 */
let pendingMainEvents: Array<{ channel: string; payload: unknown }> = []

function flushMainWindowEvents(): void {
  if (!mainWindowReady || !mainWindow || mainWindow.isDestroyed()) return
  const pending = pendingMainEvents
  pendingMainEvents = []
  for (const item of pending) mainWindow.webContents.send(item.channel, item.payload)
}

/** 主进程 → 主窗口渲染层事件(窗口未就绪时先入队,desktop:ready 后补发) */
function sendToMainWindow(channel: string, payload: unknown): void {
  if (!mainWindow || mainWindow.isDestroyed()) return
  if (!mainWindowReady) {
    pendingMainEvents.push({ channel, payload })
    return
  }
  mainWindow.webContents.send(channel, payload)
}

/** 开机自启仅在打包版写入系统;开发模式只记录配置,避免把 electron.exe 注册进自启 */
function applyLoginItem(openAtLogin: boolean): void {
  if (!app.isPackaged) return
  app.setLoginItemSettings({ openAtLogin })
}

/**
 * 首次启动时从随包携带的 enterprise.json 补全企业配置(员工端无需手填 OIDC 等)。
 * 判定基于原始 config.json, 只填空项, 不覆盖用户已保存的配置。
 */
function seedEnterpriseConfig(): void {
  runEnterpriseSeed({
    candidates: [
      process.resourcesPath ? join(process.resourcesPath, 'enterprise.json') : '',
      join(dirname(app.getPath('exe')), 'enterprise.json'),
      join(process.cwd(), 'build', 'enterprise.json')
    ].filter(Boolean),
    stored: getStoredConfig(),
    exists: existsSync,
    readFile: (file) => readFileSync(file, 'utf8'),
    update: (patch) => updateConfig(patch),
    log: (message) => console.log(message),
    warn: (message) => console.warn(message)
  })
}

function createWindow(): void {
  const theme = getConfig().theme
  mainWindowReady = false
  // 重建窗口不补发旧窗口排队的事件(新窗口的事件重新入队)
  pendingMainEvents = []
  // 任务栏/窗口图标:与托盘同源(打包后 resources/icon.png, 开发态 build/icon.png)
  const appIcon = nativeImage.createFromPath(resolveTrayIconPath())
  mainWindow = new BrowserWindow({
    width: 1360,
    height: 860,
    minWidth: 960,
    minHeight: 640,
    show: false,
    icon: appIcon.isEmpty() ? undefined : appIcon,
    // 窗口底色随配置主题取色,避免浅色模式下启动瞬间闪深色
    backgroundColor: theme === 'light' ? '#f5f7fa' : '#141414',
    titleBarStyle: 'hidden',
    titleBarOverlay: {
      ...overlayColors(theme),
      height: 36
    },
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true
    }
  })

  // Windows:部分环境下构造参数图标不生效,创建后显式再设一次(任务栏/Alt-Tab)
  if (!appIcon.isEmpty()) mainWindow.setIcon(appIcon)

  mainWindow.on('ready-to-show', () => mainWindow?.show())

  // 系统关机/注销(Windows):不会走 before-quit,在窗口级 session-end 收尾并放行退出
  mainWindow.on('session-end', () => {
    isQuitting = true
    destroyTray()
  })

  // 关闭到托盘:拦截关闭并隐藏,首次提示一次;无托盘可用时放行关闭,避免窗口失去 UI 入口
  mainWindow.on('close', (e) => {
    if (isQuitting || !getConfig().closeToTray || !hasTray()) return
    e.preventDefault()
    mainWindow?.hide()
    showFirstHideBalloon()
  })

  // 外链一律交给系统浏览器,不在应用内打开
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('http://') || url.startsWith('https://')) {
      shell.openExternal(url)
    }
    return { action: 'deny' }
  })

  if (process.env['ELECTRON_RENDERER_URL']) {
    mainWindow.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }

  mainWindow.on('closed', () => {
    mainWindow = null
    mainWindowReady = false
    // 不拦截关闭(关闭到托盘关闭/托盘不可用)时主窗销毁即退出:
    // 快速窗若仍存在会阻塞 window-all-closed,一并销毁
    if (!getConfig().closeToTray || !hasTray()) closeQuickWindow()
  })
}

/** 流式回答结束的系统通知:设置开关与窗口焦点都在主进程判定 */
function onFinishNotification(payload: Partial<FinishNotificationPayload>): void {
  const config = getConfig()
  handleFinishNotification(
    {
      sessionId: String(payload.sessionId ?? ''),
      title: String(payload.title ?? ''),
      summary: String(payload.summary ?? '')
    },
    {
      enabled: config.notifyOnFinish,
      sound: config.notifySound,
      supported: Notification.isSupported(),
      windowFocused: Boolean(mainWindow && !mainWindow.isDestroyed() && mainWindow.isFocused()),
      showWindow: showMainWindow,
      openSession: (sessionId) => sendToMainWindow('desktop:open-session', { sessionId }),
      notify: (opts) => {
        // silent:系统默认提示音会与 notifySound 的 shell.beep 双响/无法关闭;声音只由 beep 控制
        const notification = new Notification({ title: opts.title, body: opts.body, silent: true })
        notification.on('click', opts.onClick)
        notification.show()
      },
      beep: () => shell.beep(),
      warn: (message) => console.warn(message)
    }
  )
}

function registerIpc(): void {
  ipcMain.handle('config:get', () => getConfig())
  ipcMain.handle('config:set', (_e, patch: Partial<AppConfig>) => {
    const prev = getConfig()
    // 键白名单:企业/敏感字段(OIDC、服务地址、updateFeedUrl 等)从渲染层传入时忽略并告警,不报错
    const { patch: allowed, ignored } = filterUserConfigPatch(patch)
    if (ignored.length > 0) {
      console.warn(`[config] 已忽略渲染层对受限配置的写入: ${ignored.join(', ')}`)
    }
    const next = updateConfig(allowed)
    // 主题变化时同步刷新原生标题栏按钮配色
    if (allowed.theme) applyTitleBarOverlay(next.theme)
    // 桌面设置变化即时生效:快捷键变了才重注册(未变时避免无谓的注销/重注册)
    if (allowed.quickHotkey !== undefined && next.quickHotkey !== prev.quickHotkey) {
      registerQuickHotkey(next.quickHotkey)
    }
    if (allowed.launchAtLogin !== undefined) applyLoginItem(next.launchAtLogin)
    return next
  })
  ipcMain.handle('app:info', () => ({
    version: app.getVersion(),
    electron: process.versions.electron,
    node: process.versions.node,
    packaged: app.isPackaged
  }))
  ipcMain.handle('app:hotkey:status', () => getHotkeyStatus())
  ipcMain.handle('app:loginitem:get', () => ({
    openAtLogin: app.isPackaged ? app.getLoginItemSettings().openAtLogin : getConfig().launchAtLogin,
    packaged: app.isPackaged
  }))
  ipcMain.handle('app:loginitem:set', (_e, payload: { openAtLogin?: boolean }) => {
    const openAtLogin = Boolean(payload?.openAtLogin)
    updateConfig({ launchAtLogin: openAtLogin })
    applyLoginItem(openAtLogin)
    return {
      openAtLogin: app.isPackaged ? app.getLoginItemSettings().openAtLogin : openAtLogin,
      packaged: app.isPackaged
    }
  })

  // 自动更新:更新源未配置时返回可读状态(「未配置更新源」),不抛错
  ipcMain.handle('app:update:check', () => checkForUpdatesNow())
  ipcMain.handle('app:update:install', () => quitAndInstallUpdate())
  ipcMain.handle('app:update:status', () => getUpdateStatus())

  // 快速提问窗:已登录才受理并转发给主窗口;是否关窗由渲染层处理结果决定
  // (成功发送后 quick:close;流式中/取消选目录/发送失败 quick:reject 回显并保留输入)
  ipcMain.handle('quick:submit', (_e, payload: { text?: string }) => {
    const text = String(payload?.text ?? '').trim()
    if (!text) return { ok: false, error: '请输入内容' }
    if (!getIdentity()) return { ok: false, error: '请先在主界面登录' }
    sendToMainWindow('desktop:quick-prompt', { text })
    showMainWindow()
    return { ok: true }
  })
  ipcMain.handle('quick:reject', (_e, payload: { message?: string }) => {
    sendToQuickWindow('quick:error', { message: String(payload?.message ?? '发送失败,请重试') })
    return { ok: true }
  })
  ipcMain.handle('quick:close', () => {
    closeQuickWindow()
    return { ok: true }
  })

  // 渲染层就绪信号(WorkbenchLayout 挂载并注册事件监听后发):补发排队的主进程事件
  ipcMain.handle('desktop:ready', () => {
    mainWindowReady = true
    flushMainWindowEvents()
    return { ok: true }
  })

  // 通知点击/流式结束由主进程决定是否弹系统通知
  ipcMain.handle('desktop:notify:finish', (_e, payload: Partial<FinishNotificationPayload>) => {
    onFinishNotification(payload ?? {})
    return { ok: true }
  })

  // 截图提问(E): 捕获主屏整屏(MVP), 返回 PNG dataURL; 渲染层解码后走既有附件导入通路
  ipcMain.handle('desktop:screenshot', async () => {
    try {
      const display = screen.getPrimaryDisplay()
      const sources = await desktopCapturer.getSources({
        types: ['screen'],
        thumbnailSize: {
          // 物理分辨率: display.size 是 DIP, 高 DPI 屏需乘 scaleFactor 才不糊
          width: Math.max(1, Math.round(display.size.width * display.scaleFactor)),
          height: Math.max(1, Math.round(display.size.height * display.scaleFactor))
        }
      })
      const source = pickScreenSource(sources, display.id)
      if (!source || source.thumbnail.isEmpty()) {
        return { ok: false, error: screenshotFailureText('未获取到屏幕画面') }
      }
      return { ok: true, dataUrl: source.thumbnail.toDataURL() }
    } catch (err) {
      console.warn('[screenshot] 捕获失败:', (err as Error).message)
      return { ok: false, error: screenshotFailureText(err) }
    }
  })

  // 语音转写(E): 未配置 asrUrl 直接降级为可读错误; 密钥走 ensureRouterKey 自愈;
  // requestId 用于渲染层「取消转写」透传 abort(主进程另有 30s 超时兜底)
  const asrJobs = new Map<string, AbortController>()
  ipcMain.handle(
    'desktop:asr:transcribe',
    async (
      _e,
      payload?: { bytes?: Uint8Array | ArrayBuffer; name?: string; mime?: string; requestId?: string }
    ): Promise<AsrResult> => {
      const raw = payload?.bytes
      const bytes = raw instanceof ArrayBuffer ? new Uint8Array(raw) : raw instanceof Uint8Array ? raw : null
      if (!bytes || bytes.byteLength === 0) return { ok: false, error: '缺少音频数据' }
      const asrUrl = String(getConfig().asrUrl ?? '').trim()
      if (!asrUrl) return { ok: false, error: '未配置语音服务' }
      // 密钥缺失不阻断: 内网 ASR 端点可能免鉴权, 拿不到 key 时按无 Authorization 尝试
      let apiKey = ''
      try {
        apiKey = await ensureRouterKey()
      } catch (err) {
        console.warn('[asr] 未取到网关密钥, 按未鉴权端点尝试:', (err as Error).message)
      }
      const requestId = String(payload?.requestId ?? '').trim()
      const controller = new AbortController()
      if (requestId) asrJobs.set(requestId, controller)
      try {
        return await transcribeAudio({
          asrUrl,
          apiKey,
          audio: bytes,
          filename: String(payload?.name ?? '').trim() || 'voice.webm',
          mime: String(payload?.mime ?? '').trim() || 'audio/webm',
          signal: controller.signal
        })
      } finally {
        if (requestId) asrJobs.delete(requestId)
      }
    }
  )
  ipcMain.handle('desktop:asr:abort', (_e, payload?: { requestId?: string }) => {
    const requestId = String(payload?.requestId ?? '').trim()
    const controller = requestId ? asrJobs.get(requestId) : undefined
    controller?.abort()
    return { ok: Boolean(controller) }
  })

  // 企业账号 SSO:系统浏览器完成认证,凭证留在主进程,渲染层只拿到用户信息
  ipcMain.handle('sso:start', async () => {
    const user = await startSsoLogin()
    return { user }
  })
  ipcMain.handle('auth:me', () => {
    const identity = getIdentity()
    return identity ? identity.user : null
  })
  ipcMain.handle('auth:logout', () => {
    clearIdentity()
    return { success: true }
  })

  // 用量概览:取数失败直接抛错,由渲染层 catch 展示(与 config:get 等同约定)
  ipcMain.handle('usage:get', async () => {
    return getUsageSummary()
  })
}

const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  // 媒体代理 scheme 必须在 app ready 之前注册
  registerMediaScheme()

  app.on('second-instance', () => {
    showMainWindow()
  })

  app.on('before-quit', () => {
    isQuitting = true
  })

  app.on('will-quit', () => {
    destroyTray()
  })

  app.whenReady().then(async () => {
    // Windows 任务栏身份(与 electron-builder appId 一致,影响分组/通知/图标缓存键)
    if (process.platform === 'win32') app.setAppUserModelId('com.company.aibench.dashboard')
    // 凭据数据目录(credstore / identity 落盘位置)
    process.env.GATEWAY_DATA_DIR = process.env.GATEWAY_DATA_DIR ?? join(app.getPath('userData'), 'gateway')
    seedEnterpriseConfig()
    restoreIdentity()
    registerIpc()
    registerUpstreamIpc()
    registerKernelIpc()
    registerMediaProtocol()
    createWindow()

    // 桌面壳:托盘、全局快捷键、开机自启(按配置启动时生效)
    const config = getConfig()
    setupTray({
      onOpen: showMainWindow,
      onToggle: toggleMainWindow,
      onNewSession: () => {
        showMainWindow()
        sendToMainWindow('desktop:new-session', {})
      },
      onQuit: () => {
        isQuitting = true
        app.quit()
      }
    })
    registerQuickHotkey(config.quickHotkey)
    applyLoginItem(config.launchAtLogin)

    // 自动更新:更新源由企业配置注入;未配置时功能降级,仅提示「未配置更新源」
    initAutoUpdater({
      feedUrl: config.updateFeedUrl,
      autoCheck: config.autoCheckUpdate,
      notify: ({ title, body }) => {
        if (!Notification.isSupported()) return
        const notification = new Notification({ title, body, silent: true })
        notification.on('click', () => {
          // 「点击重启安装」:下载完成后点击通知即退出安装;不可安装时回到主窗口手动处理
          if (!quitAndInstallUpdate().ok) showMainWindow()
        })
        notification.show()
      },
      onStatus: (snapshot) => {
        sendToMainWindow('desktop:update-status', snapshot)
        console.log(`[update] ${snapshot.event}: ${snapshot.message}`)
      }
    })

    app.on('activate', () => {
      if (!mainWindow || mainWindow.isDestroyed()) createWindow()
    })
  })

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit()
  })
}
