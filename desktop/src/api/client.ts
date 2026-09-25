/**
 * 全部出网请求的统一传输层:渲染端只与主进程 IPC 通信,
 * 由主进程按服务注入凭证(agent JWT / router apikey / OIDC token)后转发上游。
 */

export type ServiceName = 'agent' | 'rag' | 'market' | 'router'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export interface RequestOptions {
  method?: string
  body?: unknown
  headers?: Record<string, string>
}

interface UpstreamResponse {
  status: number
  text: string
}

export async function request<T>(service: ServiceName, path: string, options: RequestOptions = {}): Promise<T> {
  const res = await window.desktop.invoke<UpstreamResponse>('upstream:request', {
    service,
    path,
    method: options.method ?? 'GET',
    body: options.body
  })
  let data: unknown = null
  try {
    data = res.text ? JSON.parse(res.text) : null
  } catch {
    // 非 JSON 响应按原文处理
  }
  if (res.status >= 400) {
    const msg =
      data && typeof data === 'object' && 'error' in (data as Record<string, unknown>)
        ? String((data as Record<string, unknown>).error)
        : `请求失败(HTTP ${res.status})`
    throw new ApiError(res.status, msg)
  }
  return data as T
}

/** 解析单个 SSE data:JSON 载荷;解析失败返回 null 并忽略 */
function parseSseFrame(frame: string): unknown | null {
  for (const line of frame.split('\n')) {
    if (!line.startsWith('data:')) continue
    const raw = line.slice(5).trim()
    if (!raw) continue
    try {
      return JSON.parse(raw)
    } catch {
      return null
    }
  }
  return null
}

interface UpstreamEventPayload {
  streamId: string
  type: 'status' | 'chunk' | 'end' | 'error'
  status?: number
  text?: string
  message?: string
  aborted?: boolean
}

/**
 * POST 一个 text/event-stream 接口并逐帧回调,直到流结束。
 * 返回 { aborted }:用户主动中止(upstream:stream:abort)时为 true,调用方据此
 * 区分「正常完成」与「中止」(如中止不发完成通知)。
 * 抛出 ApiError(HTTP 非 200)或网络错误;abort 由外部 signal 控制。
 */
export async function streamRequest<T = unknown>(
  service: ServiceName,
  path: string,
  body: unknown,
  onFrame: (payload: T) => void,
  signal?: AbortSignal
): Promise<{ aborted: boolean }> {
  const { streamId } = await window.desktop.invoke<{ streamId: string }>('upstream:stream:start', {
    service,
    path,
    body
  })

  const abort = (): void => {
    void window.desktop.invoke('upstream:stream:abort', streamId)
  }
  signal?.addEventListener('abort', abort, { once: true })
  // start 请求期间 signal 已中止:事件不会再触发,补发 abort(否则流继续、还会误报完成)
  if (signal?.aborted) void abort()

  return new Promise<{ aborted: boolean }>((resolve, reject) => {
    let buffer = ''

    const finalize = (outcome: { aborted: boolean } = { aborted: false }, err?: Error): void => {
      cleanup()
      if (err) reject(err)
      else resolve(outcome)
    }

    const unsubscribe = window.desktop.onUpstreamEvent((event: UpstreamEventPayload) => {
      if (event.streamId !== streamId) return
      if (event.type === 'status') {
        if ((event.status ?? 200) >= 400) {
          finalize(undefined, new ApiError(event.status ?? 0, `请求失败(HTTP ${event.status})`))
        }
        return
      }
      if (event.type === 'chunk') {
        buffer += event.text ?? ''
        let sep = buffer.indexOf('\n\n')
        while (sep >= 0) {
          const frame = parseSseFrame(buffer.slice(0, sep))
          if (frame !== null) onFrame(frame as T)
          buffer = buffer.slice(sep + 2)
          sep = buffer.indexOf('\n\n')
        }
        return
      }
      if (event.type === 'end') {
        // 收尾:处理未以空行结束的最后一帧
        if (buffer.trim()) {
          const frame = parseSseFrame(buffer)
          if (frame !== null) onFrame(frame as T)
        }
        finalize({ aborted: event.aborted === true })
        return
      }
      if (event.type === 'error') {
        finalize(undefined, new ApiError(event.status ?? 0, event.message ?? '请求失败'))
      }
    })

    function cleanup(): void {
      unsubscribe()
      signal?.removeEventListener('abort', abort)
    }
  })
}

export interface ProbeResult {
  ok: boolean
  status: number
  message: string
}

/** 连通性探测:任何 HTTP 响应都视为可达;网络错误视为不可达 */
export async function probe(service: ServiceName, path = '/'): Promise<ProbeResult> {
  try {
    const res = await window.desktop.invoke<UpstreamResponse>('upstream:request', {
      service,
      path,
      method: 'GET'
    })
    return { ok: true, status: res.status, message: `可达(HTTP ${res.status})` }
  } catch (err) {
    return { ok: false, status: 0, message: `不可达:${(err as Error).message}` }
  }
}
