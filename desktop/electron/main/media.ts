import { protocol, net } from 'electron'
import { getConfig } from './store'
import { freshOidcAccessToken } from './identity'

/**
 * 知识库/对话里的图片媒体代理:
 *   渲染层 CSP 只允许 'self' data: blob:(+ 本 scheme), 外部图片(内网 MinIO 等)一律拦下,
 *   故把 <img src> 统一改写成 xzmedia://fetch?u=<原始地址>, 由主进程取回再交还给渲染层。
 *   主进程 fetch 默认不带 Referer, 顺带绕过 OSS 的防盗链; 相对路径(如 /api/upload/...)
 *   按 RAG 地址补全并注入 OIDC token。
 */
export const MEDIA_SCHEME = 'xzmedia'

/** 必须在 app ready 之前调用 */
export function registerMediaScheme(): void {
  protocol.registerSchemesAsPrivileged([
    {
      scheme: MEDIA_SCHEME,
      privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true }
    }
  ])
}

async function resolveTarget(raw: string): Promise<{ url: string; headers: Record<string, string> }> {
  if (/^https?:\/\//i.test(raw)) {
    // 外链(MinIO/OSS 等): 直取, 不带 Referer
    return { url: raw, headers: {} }
  }
  const base = getConfig().ragUrl.replace(/\/+$/, '')
  const token = await freshOidcAccessToken()
  return {
    url: `${base}${raw.startsWith('/') ? raw : `/${raw}`}`,
    headers: token ? { authorization: `Bearer ${token}` } : {}
  }
}

/** 在 app ready 之后调用 */
export function registerMediaProtocol(): void {
  protocol.handle(MEDIA_SCHEME, async (request) => {
    try {
      const target = new URL(request.url).searchParams.get('u') ?? ''
      if (!target) return new Response('missing u', { status: 400 })
      const { url, headers } = await resolveTarget(target)
      const res = await net.fetch(url, { headers })
      if (!res.ok) return new Response(`upstream ${res.status}`, { status: res.status })
      const buf = Buffer.from(await res.arrayBuffer())
      return new Response(buf, {
        status: 200,
        headers: { 'content-type': res.headers.get('content-type') ?? 'application/octet-stream' }
      })
    } catch (err) {
      return new Response(`media error: ${(err as Error).message}`, { status: 502 })
    }
  })
}
