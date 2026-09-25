import { readFile, readdir, stat } from 'node:fs/promises'
import { extname, join } from 'node:path'
import { getConfig } from '../store'
import { freshAgentJwt } from '../identity'
import { resolveWithin } from './pathsafe'

/**
 * 对话产物(Canvas)读取:
 *   - 在线: 走 agent 的 /api/workspace/my/files|my/file(主进程带 agent JWT)
 *   - 离线: 直接读本地会话工作区
 * 只返回预览所需内容(文本≤1MB / 图片≤8MB 转 data URL), 不落任何本地路径给渲染层。
 */

export interface ArtifactItem {
  relPath: string
  name: string
  size: number
  modified: string
}

export interface ArtifactContent {
  kind: 'text' | 'image'
  name: string
  text?: string
  dataUrl?: string
}

const TEXT_EXTS = new Set([
  '.md', '.markdown', '.txt', '.log', '.json', '.csv', '.tsv', '.yml', '.yaml',
  '.py', '.ts', '.js', '.mjs', '.cjs', '.sh', '.ps1', '.html', '.htm', '.xml',
  '.sql', '.toml', '.ini', '.conf', '.css', '.vue', '.jsx', '.tsx', '.java', '.go'
])

const IMAGE_MIME: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.bmp': 'image/bmp',
  '.svg': 'image/svg+xml'
}

const TEXT_MAX = 1024 * 1024
const IMAGE_MAX = 8 * 1024 * 1024

export function kindOf(name: string): 'text' | 'image' | 'other' {
  const ext = extname(name).toLowerCase()
  if (IMAGE_MIME[ext]) return 'image'
  if (TEXT_EXTS.has(ext)) return 'text'
  return 'other'
}

async function agentRequest(path: string): Promise<Response> {
  const jwt = await freshAgentJwt()
  if (!jwt) throw new Error('请先完成企业 SSO 登录')
  const base = getConfig().agentUrl.replace(/\/+$/, '')
  const res = await fetch(`${base}${path}`, { headers: { authorization: `Bearer ${jwt}` } })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    let reason = `读取失败(HTTP ${res.status})`
    try {
      const parsed = JSON.parse(text) as { error?: string }
      if (parsed?.error) reason = String(parsed.error)
    } catch {
      // 保留默认原因
    }
    throw new Error(reason)
  }
  return res
}

/** 在线产物清单(agent 侧「我的工作区」) */
export async function listAgentArtifacts(limit = 200): Promise<ArtifactItem[]> {
  const res = await agentRequest(`/api/workspace/my/files?limit=${limit}`)
  const data = (await res.json()) as { files?: ArtifactItem[] }
  // 过滤运行态内部文件(.agent/tmp 等), 只留用户可读产物
  return (data.files ?? []).filter(
    (f) => !f.relPath.startsWith('.agent/') && !f.relPath.includes('/.agent/')
  )
}

/** 在线产物内容: 文本直读, 图片转 data URL */
export async function readAgentArtifact(relPath: string): Promise<ArtifactContent> {
  const name = relPath.split('/').pop() ?? relPath
  const kind = kindOf(name)
  if (kind === 'other') throw new Error('该类型暂不支持预览, 可在系统中打开')
  const res = await agentRequest(`/api/workspace/my/file?path=${encodeURIComponent(relPath)}`)
  const buf = Buffer.from(await res.arrayBuffer())
  if (buf.byteLength > (kind === 'image' ? IMAGE_MAX : TEXT_MAX)) {
    throw new Error('文件过大, 暂不支持预览')
  }
  if (kind === 'image') {
    const mime = IMAGE_MIME[extname(name).toLowerCase()] ?? 'application/octet-stream'
    return { kind, name, dataUrl: `data:${mime};base64,${buf.toString('base64')}` }
  }
  return { kind, name, text: buf.toString('utf-8') }
}

/** 离线产物清单: 本地会话工作区(按修改时间倒序, 最多 limit 条) */
export async function listLocalArtifacts(workspace: string, limit = 200): Promise<ArtifactItem[]> {
  const out: ArtifactItem[] = []
  async function walk(dir: string, depth: number): Promise<void> {
    if (depth > 3) return
    const entries = await readdir(dir, { withFileTypes: true }).catch(() => [])
    for (const entry of entries) {
      if (entry.name.startsWith('.') || entry.name === 'node_modules') continue
      const full = join(dir, entry.name)
      if (entry.isDirectory()) {
        await walk(full, depth + 1)
        continue
      }
      const info = await stat(full).catch(() => null)
      if (!info?.isFile()) continue
      out.push({
        relPath: full.slice(workspace.length + 1).replace(/\\/g, '/'),
        name: entry.name,
        size: info.size,
        modified: info.mtime.toISOString()
      })
    }
  }
  await walk(workspace, 0)
  out.sort((a, b) => (a.modified < b.modified ? 1 : -1))
  return out.slice(0, limit)
}

/** 离线产物内容(限定在会话工作区内) */
export async function readLocalArtifact(workspace: string, relPath: string): Promise<ArtifactContent> {
  const name = relPath.split('/').pop() ?? relPath
  const kind = kindOf(name)
  if (kind === 'other') throw new Error('该类型暂不支持预览, 可在系统中打开')
  const target = resolveWithin(workspace, relPath)
  const info = await stat(target)
  if (info.size > (kind === 'image' ? IMAGE_MAX : TEXT_MAX)) throw new Error('文件过大, 暂不支持预览')
  const buf = await readFile(target)
  if (kind === 'image') {
    const mime = IMAGE_MIME[extname(name).toLowerCase()] ?? 'application/octet-stream'
    return { kind, name, dataUrl: `data:${mime};base64,${buf.toString('base64')}` }
  }
  return { kind, name, text: buf.toString('utf-8') }
}
