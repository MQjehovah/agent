import { BrowserWindow, globalShortcut, screen } from 'electron'
import { join } from 'node:path'
import { registerHotkeyWith, type HotkeyResult } from './hotkey'

/** 快速提问小窗尺寸(无边框置顶) */
const QUICK_WIDTH = 520
const QUICK_HEIGHT = 72

let quickWindow: BrowserWindow | null = null
let hotkeyStatus: HotkeyResult = { ok: false, error: '尚未注册' }

export function getHotkeyStatus(): HotkeyResult {
  return hotkeyStatus
}

/** 按配置注册全局快捷键(空串=关闭);失败只返回状态,不抛错、不阻断启动 */
export function registerQuickHotkey(accelerator: string): HotkeyResult {
  const accel = accelerator.trim()
  if (!accel) {
    globalShortcut.unregisterAll()
    hotkeyStatus = { ok: false, disabled: true, error: '未设置快捷键' }
    return hotkeyStatus
  }
  hotkeyStatus = registerHotkeyWith(globalShortcut, accel, () => openQuickWindow())
  return hotkeyStatus
}

/** 唤起快速提问窗:已存在则聚焦复用,否则创建后显示 */
export function openQuickWindow(): void {
  if (quickWindow && !quickWindow.isDestroyed()) {
    quickWindow.show()
    quickWindow.focus()
    return
  }

  const { workArea } = screen.getPrimaryDisplay()
  const x = Math.round(workArea.x + (workArea.width - QUICK_WIDTH) / 2)
  const y = Math.round(workArea.y + workArea.height * 0.24)

  quickWindow = new BrowserWindow({
    width: QUICK_WIDTH,
    height: QUICK_HEIGHT,
    x,
    y,
    frame: false,
    resizable: false,
    movable: true,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    show: false,
    backgroundColor: '#161616',
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  })

  quickWindow.on('blur', () => quickWindow?.hide())
  quickWindow.on('closed', () => {
    quickWindow = null
  })
  quickWindow.once('ready-to-show', () => {
    quickWindow?.show()
    quickWindow?.focus()
  })

  if (process.env['ELECTRON_RENDERER_URL']) {
    void quickWindow.loadURL(`${process.env['ELECTRON_RENDERER_URL']}#/quick`)
  } else {
    void quickWindow.loadFile(join(__dirname, '../renderer/index.html'), { hash: '/quick' })
  }
}

/** 关闭快速提问窗(提交成功/按 Esc);下次唤起重新创建,输入框保持空态 */
export function closeQuickWindow(): void {
  if (quickWindow && !quickWindow.isDestroyed()) quickWindow.close()
  quickWindow = null
}

/** 主进程 → 快速窗事件(发送失败/流式中等错误回显);快速窗不存在时静默丢弃 */
export function sendToQuickWindow(channel: string, payload: unknown): void {
  if (quickWindow && !quickWindow.isDestroyed()) quickWindow.webContents.send(channel, payload)
}
