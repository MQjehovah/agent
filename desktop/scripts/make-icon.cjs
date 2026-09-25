/**
 * 一次性脚本: 把 src/assets/logo.svg 渲染成 build/icon.png(512x512, 含透明通道)。
 * 用法: npx electron scripts/make-icon.cjs
 */
const { app, BrowserWindow } = require('electron')
const fs = require('node:fs')
const path = require('node:path')

const root = path.join(__dirname, '..')
const svg = fs.readFileSync(path.join(root, 'src', 'assets', 'logo.svg'), 'utf8')

app.disableHardwareAcceleration()

app.whenReady().then(async () => {
  const size = 512
  const win = new BrowserWindow({
    show: false,
    width: size,
    height: size,
    frame: false,
    transparent: true,
    webPreferences: { offscreen: false }
  })
  const html = `<!doctype html><html><head><meta charset="utf-8"><style>
    html,body{margin:0;padding:0;background:transparent;overflow:hidden}
    svg{display:block;width:${size}px;height:${size}px}
  </style></head><body>${svg}</body></html>`
  await win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html))
  await new Promise((r) => setTimeout(r, 400))
  const image = await win.webContents.capturePage({ x: 0, y: 0, width: size, height: size })
  const outDir = path.join(root, 'build')
  fs.mkdirSync(outDir, { recursive: true })
  const out = path.join(outDir, 'icon.png')
  fs.writeFileSync(out, image.toPNG())
  console.log('icon written:', out, image.getSize())
  app.quit()
})
