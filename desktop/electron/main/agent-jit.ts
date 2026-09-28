// agent 账号 JIT:身份 → agent 账号映射(凭据加密存储于网关数据目录)
import { randomBytes } from 'node:crypto'
import { getConfig } from './store'
import { getCred, saveCred } from './credstore'

/** 内部错误,带 HTTP 语义状态码(仅主进程内部使用) */
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
  if (!res.ok) throw new AuthError(401, `agent 登录失败(HTTP ${res.status})`)
  const data = (await res.json()) as { token: string }
  // agent JWT 7 天有效, 提前 1 天刷新
  adminJwtCache = { jwt: data.token, expiresAt: Date.now() + 6 * 24 * 3_600_000 }
  return data.token
}

/**
 * 管理员 JWT:优先环境变量凭据;若 admin 密码已被本网关 JIT 轮换,
 * 回退使用凭据库中 admin 的托管密码(鸡生蛋问题的解法)。
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
    throw new AuthError(502, 'agent 管理员凭据无效(检查 AGENT_ADMIN_USER/PASSWORD)')
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
    throw new AuthError(502, `agent 管理接口拒绝(HTTP ${res.status}):${await res.text().catch(() => '')}`)
  }
  if (!res.ok) throw new AuthError(502, `agent 管理接口失败(HTTP ${res.status})`)
  return res.json()
}

function randomPassword(): string {
  return 'Ai' + randomBytes(12).toString('base64url') + '!7'
}

/**
 * Map SSO roles to an agent rbac role. 'admin' when SSO grants admin, else the
 * least-privileged 'default'. Callers apply elevate-only semantics (never demote).
 */
export function mapAgentRole(roles: string[] = []): string {
  return roles.some((r) => String(r).toLowerCase() === 'admin') ? 'admin' : 'default'
}

/**
 * 按工号 find-or-provision agent 账号,并取得该用户的 agent JWT。
 * 策略:
 *   - 首次登录:agent 侧建号(随机密码)或对存量账号重置一次密码 → 网关加密保存凭据
 *   - 后续登录:用保存的凭据登录(不再轮换,避免破坏存量账号)
 *   - 凭据失效(密码被外部改动) → 重置一次并更新存储
 * 账号模型(姓名/工号分离):name=姓名, work_id=工号(身份键, 市场代授权用它);
 * 登录即按 SSO 权威源回写姓名/部门。
 */
export async function ensureAgentJwt(
  sub: string,
  name: string,
  status: string,
  roles: string[] = [],
  department = ''
): Promise<string> {
  const display = name && name !== sub ? name : sub
  const usersRes = await agentCall('/api/rbac/users')
  const list: Array<Record<string, unknown>> =
    Array.isArray(usersRes) ? usersRes : (usersRes?.users ?? [])
  const byIdentity = (u: Record<string, unknown>) =>
    String(u.work_id ?? '') === sub || String(u.name) === sub
  const found = list.find(byIdentity)
  const stored = getCred(sub)

  if (!found) {
    // JIT 开通: 默认 default 角色("只能对话"), 特殊角色由管理员在 agent 侧调整
    const password = randomPassword()
    await agentCall('/api/rbac/users', {
      method: 'POST',
      body: JSON.stringify({ name: display, work_id: sub, password, role: mapAgentRole(roles), department })
    })
    const usersNow: Array<Record<string, unknown>> =
      Array.isArray(usersRes) ? [] : ((await agentCall('/api/rbac/users'))?.users ?? [])
    const created = usersNow.find(byIdentity)
    if (created) saveCred(sub, { agentUserId: created.id as number | string, agentPassword: password })
    console.log(`[jit] 开通 agent 账号:${sub}(${display})`)
  } else if (String(found.status ?? 'active') !== 'active') {
    throw new AuthError(403, '账号已被禁用,请联系管理员')
  }

  // 有网关保管的凭据 → 先尝试直接登录
  // Elevate-only role sync: promote an existing account to admin when SSO grants
  // admin (never demote, to avoid locking out a manually-managed account).
  // 姓名/部门按 SSO 权威源回写(登录即同步, 与 SSO 登录同语义)。
  if (found) {
    const desired = mapAgentRole(roles)
    if (desired === 'admin' && String(found.role ?? 'default') !== 'admin') {
      await agentCall(`/api/rbac/users/${found.id}`, {
        method: 'PUT',
        body: JSON.stringify({ role: 'admin' })
      })
      console.log(`[jit] elevate agent role to admin: ${sub}`)
    }
    const patch: Record<string, string> = {}
    if (display && String(found.name ?? '') !== display) patch.name = display
    if (department && String(found.department ?? '') !== department) patch.department = department
    if (Object.keys(patch).length) {
      await agentCall(`/api/rbac/users/${found.id}`, {
        method: 'PUT',
        body: JSON.stringify(patch)
      })
      console.log(`[jit] 回写姓名/部门: ${sub} ${JSON.stringify(patch)}`)
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
    console.warn(`[jit] 存储凭据失效,重置 agent 密码后重试: ${sub}`)
  }

  // 凭据缺失或失效 → 由 admin 重置一次,更新存储
  const usersNow: Array<Record<string, unknown>> =
    Array.isArray(usersRes) ? usersRes : ((await agentCall('/api/rbac/users'))?.users ?? [])
  const target = found ?? usersNow.find(byIdentity)
  if (!target || target.id === undefined || target.id === null) {
    throw new AuthError(502, 'agent 用户开通后未找到 id')
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
  if (!resetRes.ok) throw new AuthError(502, `agent 密码重置失败(HTTP ${resetRes.status})`)
  saveCred(sub, { agentUserId: userId, agentPassword: random })

  const loginRes = await fetch(`${agentBase()}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: sub, password: random })
  })
  if (!loginRes.ok) throw new AuthError(502, `agent 登录失败(HTTP ${loginRes.status})`)
  const login = (await loginRes.json()) as { token: string }
  return login.token
}
