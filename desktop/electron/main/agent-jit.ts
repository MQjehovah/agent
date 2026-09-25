// agent 璐﹀彿 JIT:韬唤 鈫?agent 璐﹀彿鏄犲皠(鍑嵁鍔犲瘑瀛樺偍浜庣綉鍏虫暟鎹洰褰?
import { randomBytes } from 'node:crypto'
import { getConfig } from './store'
import { getCred, saveCred } from './credstore'

/** 鍐呴儴閿欒,甯?HTTP 璇箟鐘舵€佺爜(浠呬富杩涚▼鍐呴儴浣跨敤) */
export class AuthError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

let adminJwtCache: { jwt: string; expiresAt: number } | null = null

function agentBase(): string {
  return getConfig().agentUrl.replace(/\/+$/, '')
}

async function agentLogin(username: string, password: string): Promise<string> {
  const res = await fetch(`${agentBase()}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password })
  })
  if (!res.ok) throw new AuthError(401, `agent 鐧诲綍澶辫触(HTTP ${res.status})`)
  const data = (await res.json()) as { token: string }
  // agent JWT 7 澶╂湁鏁?鎻愬墠 1 灏忔椂鍒锋柊
  adminJwtCache = { jwt: data.token, expiresAt: Date.now() + 6 * 24 * 3_600_000 }
  return data.token
}

/**
 * 绠＄悊鍛?JWT:浼樺厛鐜鍙橀噺鍑嵁;鑻?admin 瀵嗙爜宸茶鏈綉鍏?JIT 杞崲,
 * 鍥為€€浣跨敤鍑嵁搴撲腑 admin 鐨勬墭绠″瘑鐮?楦＄敓铔嬮棶棰樼殑瑙ｆ硶)銆?
 */
export async function agentAdminJwt(): Promise<string> {
  if (adminJwtCache && Date.now() < adminJwtCache.expiresAt) return adminJwtCache.jwt
  const envUser = process.env.AGENT_ADMIN_USER ?? 'admin'
  const envPass = process.env.AGENT_ADMIN_PASSWORD ?? ''
  try {
    return await agentLogin(envUser, envPass)
  } catch {
    const stored = getCred(envUser)
    if (stored) return await agentLogin(envUser, stored.agentPassword)
    throw new AuthError(502, 'agent 绠＄悊鍛樺嚟鎹棤鏁?妫€鏌?AGENT_ADMIN_USER/PASSWORD)')
  }
}

async function agentCall(path: string, init: RequestInit = {}): Promise<any> {
  // 服务间专用凭证优先(AGENT_SERVICE_TOKEN,不再借用 admin 密码);未配置时回退 admin JWT
  const svcToken = process.env.AGENT_SERVICE_TOKEN ?? getConfig().agentServiceToken ?? ''
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (svcToken) headers['X-Service-Token'] = svcToken
  else headers['Authorization'] = `Bearer ${await agentAdminJwt()}`
  Object.assign(headers, init.headers ?? {})
  const res = await fetch(`${agentBase()}${path}`, { ...init, headers })
  if (res.status === 401 || res.status === 403) {
    adminJwtCache = null
    throw new AuthError(502, `agent 绠＄悊鎺ュ彛鎷掔粷(HTTP ${res.status}):${await res.text().catch(() => '')}`)
  }
  if (!res.ok) throw new AuthError(502, `agent 绠＄悊鎺ュ彛澶辫触(HTTP ${res.status})`)
  return res.json()
}

function randomPassword(): string {
  return 'Ai' + randomBytes(12).toString('base64url') + '!7'
}

/**
 * 鎸夊伐鍙?find-or-provision agent 璐﹀彿,骞跺彇寰楄鐢ㄦ埛鐨?agent JWT銆?
 * 绛栫暐:
 *   - 棣栨鐧诲綍:agent 渚у缓鍙?闅忔満瀵嗙爜)鎴栧瀛橀噺璐﹀彿閲嶇疆涓€娆″瘑鐮?鈫?缃戝叧鍔犲瘑淇濆瓨鍑嵁
 *   - 鍚庣画鐧诲綍:鐢ㄤ繚瀛樼殑鍑嵁鐧诲綍(涓嶅啀杞崲,閬垮厤鐮村潖瀛橀噺璐﹀彿)
 *   - 鍑嵁澶辨晥(瀵嗙爜琚閮ㄦ敼鍔?鈫?閲嶇疆涓€娆″苟鏇存柊瀛樺偍
 */
/**
 * Map SSO roles to an agent rbac role. 'admin' when SSO grants admin, else the
 * least-privileged 'default'. Callers apply elevate-only semantics (never demote).
 */
export function mapAgentRole(roles: string[] = []): string {
  return roles.some((r) => String(r).toLowerCase() === 'admin') ? 'admin' : 'default'
}

export async function ensureAgentJwt(
  sub: string,
  name: string,
  status: string,
  roles: string[] = []
): Promise<string> {
  const usersRes = await agentCall('/api/rbac/users')
  const list: Array<Record<string, unknown>> =
    Array.isArray(usersRes) ? usersRes : (usersRes?.users ?? [])
  const found = list.find((u) => String(u.name) === sub)
  const stored = getCred(sub)

  if (!found) {
    // JIT 寮€閫?榛樿 default 瑙掕壊("鍙兘瀵硅瘽"),鐗规畩瑙掕壊鐢辩鐞嗗憳鍦?agent 渚ц皟鏁?
    const password = randomPassword()
    await agentCall('/api/rbac/users', {
      method: 'POST',
      body: JSON.stringify({ name: sub, password, role: mapAgentRole(roles), department: '' })
    })
    const usersNow: Array<Record<string, unknown>> =
      Array.isArray(usersRes) ? [] : ((await agentCall('/api/rbac/users'))?.users ?? [])
    const created = usersNow.find((u) => String(u.name) === sub)
    if (created) saveCred(sub, { agentUserId: created.id as number | string, agentPassword: password })
    console.log(`[jit] 寮€閫?agent 璐﹀彿:${sub}(${name})`)
  } else if (String(found.status ?? 'active') !== 'active') {
    throw new AuthError(403, '璐﹀彿宸茶绂佺敤,璇疯仈绯荤鐞嗗憳')
  }

  // 鏈夌綉鍏充繚绠＄殑鍑嵁 鈫?鍏堝皾璇曠洿鎺ョ櫥褰?
  // Elevate-only role sync: promote an existing account to admin when SSO grants
  // admin (never demote, to avoid locking out a manually-managed account).
  if (found) {
    const desired = mapAgentRole(roles)
    if (desired === 'admin' && String(found.role ?? 'default') !== 'admin') {
      await agentCall(`/api/rbac/users/${found.id}`, {
        method: 'PUT',
        body: JSON.stringify({ role: 'admin' })
      })
      console.log(`[jit] elevate agent role to admin: ${sub}`)
    }
  }

  if (stored) {
    const loginRes = await fetch(`${agentBase()}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: sub, password: stored.agentPassword })
    })
    if (loginRes.ok) {
      const login = (await loginRes.json()) as { token: string }
      return login.token
    }
    console.warn(`[jit] 瀛樺偍鍑嵁澶辨晥,閲嶇疆 agent 瀵嗙爜鍚庨噸璇?${sub}`)
  }

  // 鍑嵁缂哄け鎴栧け鏁?鈫?鐢?admin 閲嶇疆涓€娆?鏇存柊瀛樺偍
  const usersNow: Array<Record<string, unknown>> =
    Array.isArray(usersRes) ? usersRes : ((await agentCall('/api/rbac/users'))?.users ?? [])
  const target = found ?? usersNow.find((u) => String(u.name) === sub)
  if (!target || target.id === undefined || target.id === null) {
    throw new AuthError(502, 'agent 鐢ㄦ埛寮€閫氬悗鏈壘鍒?id')
  }
  const userId = target.id as string | number

  const random = randomPassword()
  const resetHeaders: Record<string, string> = { 'Content-Type': 'application/json' }
  const svcToken = process.env.AGENT_SERVICE_TOKEN ?? getConfig().agentServiceToken ?? ''
  if (svcToken) resetHeaders['X-Service-Token'] = svcToken
  else resetHeaders['Authorization'] = `Bearer ${await agentAdminJwt()}`
  const resetRes = await fetch(`${agentBase()}/api/auth/set-password`, {
    method: 'POST',
    headers: resetHeaders,
    body: JSON.stringify({ user_id: userId, password: random })
  })
  if (!resetRes.ok) throw new AuthError(502, `agent 瀵嗙爜閲嶇疆澶辫触(HTTP ${resetRes.status})`)
  saveCred(sub, { agentUserId: userId, agentPassword: random })

  const loginRes = await fetch(`${agentBase()}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: sub, password: random })
  })
  if (!loginRes.ok) throw new AuthError(502, `agent 鐧诲綍澶辫触(HTTP ${loginRes.status})`)
  const login = (await loginRes.json()) as { token: string }
  return login.token
}
