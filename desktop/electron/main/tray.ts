import { app, Menu, nativeImage, Tray } from 'electron'
import { existsSync } from 'node:fs'
import { join } from 'node:path'

export interface TrayHandlers {
  /** 菜单「打开主界面」:恒显示并聚焦 */
  onOpen(): void
  /** 左键单击:切换主窗显示/收起 */
  onToggle(): void
  onNewSession(): void
  onQuit(): void
}

let tray: Tray | null = null
/** 首次隐藏到托盘的气泡只提示一次 */
let balloonShown = false

/**
 * 托盘图标:打包后取 resources/icon.png(electron-builder extraResources),
 * 开发态回退仓库 build/icon.png。
 */
export function resolveTrayIconPath(): string {
  const packaged = process.resourcesPath ? join(process.resourcesPath, 'icon.png') : ''
  if (packaged && existsSync(packaged)) return packaged
  return join(app.getAppPath(), 'build', 'icon.png')
}

export function setupTray(handlers: TrayHandlers): void {
  if (tray) return
  const iconPath = resolveTrayIconPath()
  const image = nativeImage.createFromPath(iconPath)
  if (image.isEmpty()) {
    // 图标缺失不致命:跳过托盘,应用其余能力不受影响
    console.warn(`[tray] 托盘图标加载失败,已跳过托盘:${iconPath}`)
    return
  }
  // Windows 托盘按 16px 渲染,其它平台托盘图标惯用 32px
  const size = process.platform === 'win32' ? 16 : 32
  try {
    tray = new Tray(image.resize({ width: size, height: size }))
  } catch (err) {
    console.warn(`[tray] 创建托盘失败:${(err as Error).message}`)
    tray = null
    return
  }
  tray.setToolTip('零号员工')
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: '打开主界面', click: () => handlers.onOpen() },
      { label: '新建会话', click: () => handlers.onNewSession() },
      { type: 'separator' },
      { label: '退出', click: () => handlers.onQuit() }
    ])
  )
  tray.on('click', () => handlers.onToggle())
}

/** 托盘是否可用(初始化失败/已被销毁时为 false,关闭到托盘需据此降级) */
export function hasTray(): boolean {
  return tray !== null && !tray.isDestroyed()
}

/** 首次关闭到托盘时提示一次(仅 Windows 支持气泡,其它平台静默) */
export function showFirstHideBalloon(): void {
  if (balloonShown || !tray) return
  balloonShown = true
  if (process.platform !== 'win32') return
  try {
    tray.displayBalloon({
      title: '零号员工',
      content: '应用仍在后台运行,点击托盘图标可重新打开'
    })
  } catch {
    // 部分系统版本不支持气泡,忽略
  }
}

export function destroyTray(): void {
  tray?.destroy()
  tray = null
}
