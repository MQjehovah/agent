import { isAbsolute, resolve, sep } from 'node:path'

/**
 * 工作区路径安全：file/terminal 等工具的路径必须经 resolveWithin 校验，
 * 防止逃逸出工作区根目录。纯字符串运算，不做任何 fs 访问。
 */

/** 路径越界专用错误类型，便于调用方精确捕获 */
export class PathEscapeError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'PathEscapeError'
  }
}

/** 归一化为绝对路径形式，并去掉结尾分隔符，便于前缀比较（仅 win32 大小写折叠，POSIX 文件系统大小写敏感） */
function norm(p: string): string {
  const r = resolve(p)
  return (process.platform === 'win32' ? r.toLowerCase() : r).replace(/[\\/]+$/, '')
}

/** 判断 target 是否位于 workspace 内（含 workspace 本身），workspace 必须为绝对路径 */
export function isWithin(workspace: string, target: string): boolean {
  if (!isAbsolute(workspace)) throw new Error('workspace 必须是绝对路径')
  const w = norm(workspace)
  const t = norm(target)
  // 单一边界判断：win32 下 sep 为 \（resolve 已把 / 归一为 \），POSIX 下 resolve 不归一 \，
  // 字面反斜杠不会被误当作分隔符，天然杜绝 /home/u/ws\evil 被误判 inside
  return t === w || t.startsWith(w + sep)
}

/** 将 target 解析为 workspace 内的绝对路径，越界则抛 PathEscapeError */
export function resolveWithin(workspace: string, target: string): string {
  if (!isAbsolute(workspace)) throw new Error('workspace 必须是绝对路径')
  const abs = resolve(workspace, target)
  if (!isWithin(workspace, abs)) throw new PathEscapeError(`路径越界: ${target} 不在工作区内`)
  return abs
}
