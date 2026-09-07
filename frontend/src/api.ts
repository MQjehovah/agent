/** API 传输层:统一 Bearer 注入 + SSE 流解析 */

const TOKEN_KEY = 'agent_jwt'
const ROLE_KEY = 'agent_role'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function setToken(t: string) {
  localStorage.setItem(TOKEN_KEY, t)
  // 从 agent JWT payload 顺带还原角色（SSO 登录等场景前端无 user.role 回包）
  const role = roleFromJwt(t)
  if (role) setRole(role)
}

/** 仅解 JWT payload（不验签）取 role：用于前端菜单/路由展示级判断，后端仍强制鉴权 */
function roleFromJwt(token: string): string {
  try {
    const part = token.split('.')[1] ?? ''
    const json = atob(part.replace(/-/g, '+').replace(/_/g, '/'))
    const payload = JSON.parse(json)
    return payload && typeof payload.role === 'string' ? payload.role : ''
  } catch {
    return ''
  }
}

export function getRole(): string {
  return localStorage.getItem(ROLE_KEY) ?? ''
}

export function setRole(role: string) {
  localStorage.setItem(ROLE_KEY, role)
}

export function isAdmin(): boolean {
  return getRole() === 'admin'
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(ROLE_KEY)
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function api<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    ...(options.headers as Record<string, string> | undefined)
  }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  if (options.body && typeof options.body === 'string') headers['Content-Type'] = 'application/json'

  const res = await fetch(path, { ...options, headers })
  if (res.status === 401) {
    clearToken()
    location.hash = '#/login'
    throw new ApiError(401, '登录已过期,请重新登录')
  }
  const text = await res.text()
  let data: unknown = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    /* 非 JSON 按原文 */
  }
  if (!res.ok) {
    const msg =
      data && typeof data === 'object' && 'error' in (data as Record<string, unknown>)
        ? String((data as Record<string, unknown>).error)
        : `请求失败(HTTP ${res.status})`
    throw new ApiError(res.status, msg)
  }
  return data as T
}

/** POST JSON 快捷 */
export function post<T = any>(path: string, body: unknown): Promise<T> {
  return api<T>(path, { method: 'POST', body: JSON.stringify(body) })
}

export function patch<T = any>(path: string, body: unknown): Promise<T> {
  return api<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
}

export function del<T = any>(path: string): Promise<T> {
  return api<T>(path, { method: 'DELETE' })
}

/** SSE 流式接口(POST):逐帧解析 data: JSON 并回调,done/error 时结束 */
export async function streamChat(
  body: { message: string; session_id?: string },
  onEvent: (event: { type: string; content?: string; data?: any }) => void,
  signal?: AbortSignal
): Promise<void> {
  const res = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify(body),
    signal
  })
  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => '')
    throw new ApiError(res.status, text.slice(0, 200) || `流式请求失败(HTTP ${res.status})`)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let sep = buffer.indexOf('\n\n')
    while (sep >= 0) {
      const frame = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      sep = buffer.indexOf('\n\n')
      for (const line of frame.split('\n')) {
        if (!line.startsWith('data:')) continue
        const raw = line.slice(5).trim()
        if (!raw) continue
        try {
          onEvent(JSON.parse(raw))
        } catch {
          /* 忽略坏帧 */
        }
      }
    }
  }
}
