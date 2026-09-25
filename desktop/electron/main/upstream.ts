import { randomBytes } from 'node:crypto'
import { ipcMain, type IpcMainInvokeEvent } from 'electron'
import { getConfig } from './store'
import { freshAgentJwt, freshOidcAccessToken, getIdentity, ensureRouterKey } from './identity'

/**
 * 上游代理(IPC 直达,无本地 HTTP):
 *   渲染层 invoke('upstream:request'|'upstream:stream:start') → 主进程注入凭证 → fetch 上游。
 *   凭证策略:agent → agent JWT;router → apikey;rag/market → OIDC access_token。
 *   SSE 流以 streamId 为键,经 'upstream:event' 通道推送原始文本块,abort 通道可中断。
 */

export type ServiceName = 'agent' | 'rag' | 'market' | 'router'

const SERVICE_FIELD: Record<ServiceName, 'agentUrl' | 'ragUrl' | 'marketUrl' | 'routerUrl'> = {
  agent: 'agentUrl',
  rag: 'ragUrl',
  market: 'marketUrl',
  router: 'routerUrl'
}

async function authFor(service: ServiceName): Promise<string | null> {
  const identity = getIdentity()
  if (!identity) return null
  // agent JWT 默认 12h, 请求前按需用本机保管的凭据续期
  if (service === 'agent') return freshAgentJwt()
  if (service === 'router') {
    // routerKey 可能因登录时交换降级而缺失:走 ensureRouterKey 自愈;失败仍回退无鉴权(由上游 401 提示)
    try {
      return await ensureRouterKey()
    } catch (err) {
      console.warn('[upstream] router 凭据获取失败:', (err as Error).message)
      return null
    }
  }
  // rag/market 走 OIDC access_token:请求前按需刷新(refresh_token 12h,access_token 1h)
  return freshOidcAccessToken()
}

function upstreamUrl(service: ServiceName, path: string): string | null {
  const field = SERVICE_FIELD[service]
  if (!field) return null
  const base = getConfig()[field].replace(/\/+$/, '')
  return `${base}${path.startsWith('/') ? path : `/${path}`}`
}

export interface UpstreamRequestPayload {
  service: ServiceName
  path: string
  method?: string
  body?: unknown
}

export interface UpstreamResponse {
  status: number
  text: string
}

async function handleRequest(_event: IpcMainInvokeEvent, payload: UpstreamRequestPayload): Promise<UpstreamResponse> {
  const url = upstreamUrl(payload.service, payload.path)
  if (!url) throw new Error(`未知的服务:${payload.service}`)

  const headers: Record<string, string> = {}
  const token = await authFor(payload.service)
  if (token) headers.authorization = `Bearer ${token}`
  if (payload.body !== undefined) headers['content-type'] = 'application/json'

  const res = await fetch(url, {
    method: payload.method ?? 'GET',
    headers,
    body: payload.body !== undefined ? JSON.stringify(payload.body) : undefined
  })
  return { status: res.status, text: await res.text() }
}

// ---- SSE 流式 ----

interface StreamState {
  controller: AbortController
}

const streams = new Map<string, StreamState>()

async function handleStreamStart(
  event: IpcMainInvokeEvent,
  payload: UpstreamRequestPayload & { streamId?: string }
): Promise<{ streamId: string }> {
  const url = upstreamUrl(payload.service, payload.path)
  if (!url) throw new Error(`未知的服务:${payload.service}`)

  const streamId = payload.streamId || randomBytes(8).toString('hex')
  const headers: Record<string, string> = { accept: 'text/event-stream' }
  const token = await authFor(payload.service)
  if (token) headers.authorization = `Bearer ${token}`
  if (payload.body !== undefined) headers['content-type'] = 'application/json'

  const controller = new AbortController()
  streams.set(streamId, { controller })

  const send = (data: Record<string, unknown>): void => {
    if (!event.sender.isDestroyed()) {
      event.sender.send('upstream:event', { streamId, ...data })
    }
  }

  // 异步泵:不阻塞 invoke 返回 streamId
  void (async () => {
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers,
        body: payload.body !== undefined ? JSON.stringify(payload.body) : undefined,
        signal: controller.signal
      })
      if (!res.ok || !res.body) {
        const text = await res.text().catch(() => '')
        let message = `请求失败(HTTP ${res.status})`
        try {
          const parsed = JSON.parse(text) as Record<string, unknown>
          if (parsed?.error) message = String(parsed.error)
        } catch {
          // 保留默认消息
        }
        send({ type: 'error', status: res.status, message })
        return
      }

      send({ type: 'status', status: res.status })
      const decoder = new TextDecoder()
      // 中止可能发生在流读取途中(break)或请求阶段(catch),两条路径都要带 aborted 标记
      let aborted = false
      for await (const chunk of res.body) {
        if (controller.signal.aborted) {
          aborted = true
          break
        }
        send({ type: 'chunk', text: decoder.decode(chunk as Uint8Array, { stream: true }) })
      }
      send(aborted ? { type: 'end', aborted: true } : { type: 'end' })
    } catch (err) {
      if (!controller.signal.aborted) {
        send({ type: 'error', status: 0, message: (err as Error).message })
      } else {
        send({ type: 'end', aborted: true })
      }
    } finally {
      streams.delete(streamId)
    }
  })()

  return { streamId }
}

export function registerUpstreamIpc(): void {
  ipcMain.handle('upstream:request', handleRequest)
  ipcMain.handle('upstream:stream:start', handleStreamStart)
  ipcMain.handle('upstream:stream:abort', (_e, streamId: string) => {
    streams.get(streamId)?.controller.abort()
  })
}
