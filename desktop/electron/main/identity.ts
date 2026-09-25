import { createServer, type Server } from 'node:http'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { app, shell } from 'electron'
import { createRemoteJWKSet, jwtVerify } from 'jose'
import { getConfig } from './store'
import { resolveOidcIssuer, resolveOidcClientId, resolveOidcClientSecret } from './oidc-config'
import { createSsoFlow } from './sso-flow'
import { ensureAgentJwt } from './agent-jit'
import { encrypt, decrypt } from './credstore'
import { createRouterCredentials } from './router-credentials'

/**
 * 身份与凭据管理(仅主进程):
 *   - OIDC SSO 登录(系统浏览器 + loopback 回调),持有 id/access/refresh token
 *   - router access_token:用 id_token 走 RFC 8693 token-exchange 换取(供 /api/me/* 调用)
 *   - router apikey:用 router access_token 调 router admin /api/me/key 自取(sk-,供 gateway/agent)
 *   - agent JWT:按工号 JIT 开号换取
 *   - rag/market 复用 OIDC access_token;agent 用 agent JWT;router 用 apikey
 * 全部凭据只在主进程内存与加密落盘,渲染层永远拿不到。
 */

export interface IdentityUser {
  id: string
  name: string
  department?: string
  /** SSO 角色(id_token 的 roles claim);用于 agent JIT 账号角色透传(仅升权) */
  roles?: string[]
  /** 钉钉 userId（id_token 的 dingtalk claim；缺省表示 SSO 未提供） */
  dingtalk?: string
}

export interface OidcTokens {
  idToken: string
  accessToken: string
  refreshToken: string
  /** access_token 过期时间戳(ms) */
  expiresAt: number
}

export interface Identity {
  user: IdentityUser
  oidc: OidcTokens | null
  /** router apikey(sk-),供 gateway 调用与本地 agent 使用 */
  routerKey: string
  /** router access_token,仅供 router admin /api/me/* 的 Bearer 使用 */
  routerToken?: string
  /** routerToken 过期时间戳(ms) */
  routerTokenExpiresAt?: number
  agentJwt: string
}

let current: Identity | null = null

export function getIdentity(): Identity | null {
  return current
}

// ---- 持久化(加密落盘,应用重启免登录) ----

function identityFile(): string {
  const dir = process.env.GATEWAY_DATA_DIR ?? join(app.getPath('userData'), 'gateway')
  return join(dir, 'identity.json')
}

function saveIdentity(identity: Identity): void {
  current = identity
  try {
    mkdirSync(dirname(identityFile()), { recursive: true })
    writeFileSync(identityFile(), encrypt(JSON.stringify(identity)), 'utf-8')
  } catch (err) {
    console.warn('[identity] 持久化失败(仅本进程内有效):', (err as Error).message)
  }
}

export function clearIdentity(): void {
  current = null
  try {
    if (existsSync(identityFile())) writeFileSync(identityFile(), '')
  } catch {
    // 忽略删除失败
  }
}

/** 应用启动时恢复身份;OIDC 过期则尝试刷新,失败视为未登录 */
export function restoreIdentity(): void {
  try {
    if (!existsSync(identityFile())) return
    const raw = readFileSync(identityFile(), 'utf-8').trim()
    if (!raw) return
    const identity = JSON.parse(decrypt(raw)) as Identity
    current = identity
    void ensureFreshOidc().catch(() => {
      // 刷新失败:保留身份,由鉴权注入处决定(无 token → 上游 401 → 提示重登)
    })
  } catch {
    current = null
  }
}

// ---- OIDC ----

function issuer(): string {
  return resolveOidcIssuer(process.env.OIDC_ISSUER, getConfig().oidcIssuer)
}

function oidcClient(): { clientId: string; clientSecret: string } {
  const cfg = getConfig()
  return {
    clientId: resolveOidcClientId(process.env.OIDC_CLIENT_ID, cfg.oidcClientId),
    clientSecret: resolveOidcClientSecret(process.env.OIDC_CLIENT_SECRET, cfg.oidcClientSecret)
  }
}

async function ensureFreshOidc(): Promise<void> {
  const oidc = current?.oidc
  if (!oidc) return
  if (oidc.expiresAt > Date.now() + 60_000) return
  if (!oidc.refreshToken) throw new Error('无 refresh_token')

  const data = await ssoFlow.refresh(oidc.refreshToken)
  if (!data.access_token || !current) throw new Error('刷新响应缺少 access_token')
  current.oidc = {
    idToken: data.id_token ?? oidc.idToken,
    accessToken: data.access_token,
    refreshToken: data.refresh_token ?? oidc.refreshToken,
    expiresAt: Date.now() + (data.expires_in ?? 3600) * 1000
  }
  saveIdentity(current)
}

/**
 * 取当前可用的 OIDC access_token:临近过期(60s 内)自动用 refresh_token 续期。
 * 供上游代理在每次请求前调用 —— access_token 只有 1 小时,长驻进程不能只在启动刷一次。
 * 刷新失败不抛异常(返回现有 token 或 null),由上游 401 走「重新登录」提示。
 */
export async function freshOidcAccessToken(): Promise<string | null> {
  try {
    await ensureFreshOidc()
  } catch {
    // 忽略:refresh_token 失效时保留现状,由上游 401 决定后续
  }
  return current?.oidc?.accessToken ?? null
}

/** 解析 JWT 的 exp(ms);解析失败返回 0 */
function jwtExpMs(token: string): number {
  try {
    const part = token.split('.')[1] ?? ''
    const payload = JSON.parse(Buffer.from(part, 'base64url').toString('utf-8')) as { exp?: number }
    return payload.exp ? payload.exp * 1000 : 0
  } catch {
    return 0
  }
}

/**
 * 取可用的 agent JWT:临近过期(5 分钟内)或已过期时,用本网关保管的凭据重新登录换取。
 * agent 侧会话默认 12h,长驻的桌面端不能只在登录时取一次,否则第二天全线 401。
 * 换不到时仍返回旧 token,由上游 401 提示重新登录。
 */
export async function freshAgentJwt(): Promise<string | null> {
  const identity = current
  if (!identity) return null
  const jwt = identity.agentJwt
  if (jwt && jwtExpMs(jwt) - Date.now() > 5 * 60_000) return jwt
  try {
    const next = await ensureAgentJwt(
      String(identity.user.id),
      identity.user.name,
      'active',
      identity.user.roles ?? []
    )
    if (current) {
      current.agentJwt = next
      saveIdentity(current)
    }
    return next
  } catch (err) {
    console.warn('[identity] agent JWT 续期失败:', (err as Error).message)
    return jwt || null
  }
}

// ---- router 凭据(token 交换 + apikey 自取) ----

const routerCredentials = createRouterCredentials<Identity>({
  getIdentity: () => current,
  saveIdentity,
  ensureFreshOidc,
  issuer,
  oidcClient,
  routerAdminUrl: () => getConfig().routerAdminUrl
})

/**
 * 取新鲜 router token:未过期(>60s 余量)直接返回;过期/缺失时交换;并发调用合并为一次交换(单飞);
 * 交换失败但旧 token 未过期时回退旧值,否则抛错。
 */
export function freshRouterToken(): Promise<string> {
  return routerCredentials.freshRouterToken()
}

/** 401 自愈:清零 token 过期时间并落盘后强制重换(usage 首次 401 时重试用) */
export function renewRouterToken(): Promise<string> {
  return routerCredentials.renewRouterToken()
}

/** 取 router apikey:已有则直接返回;否则调 router admin /api/me/key(401 时重换 token 重试一次) */
export function fetchRouterKey(): Promise<string> {
  return routerCredentials.fetchRouterKey()
}

/** 兼容既有调用点:语义等同 fetchRouterKey() */
export function ensureRouterKey(): Promise<string> {
  return fetchRouterKey()
}

// ---- SSO 登录(系统浏览器 + loopback 回调) ----

/** SSO 端注册的回调地址,必须与其完全一致(精确匹配);可用环境变量覆盖 */
function registeredRedirectUri(): string {
  return process.env.OIDC_REDIRECT_URI ?? 'http://127.0.0.1:8090/api/auth/oidc/callback'
}

/** 授权码 + PKCE 流程(授权 URL 与 token 请求在 sso-flow 中,回环服务留在主进程) */
const ssoFlow = createSsoFlow({
  issuer,
  oidcClient,
  redirectUri: registeredRedirectUri,
  openExternal: (url) => shell.openExternal(url),
  beginCallback: async (state, timeoutMs) => {
    // 解析注册的回调地址:固定端口 + 固定路径(SSO 端精确匹配,不能用随机端口)
    const parsed = new URL(registeredRedirectUri())
    const port = Number(parsed.port) || (parsed.protocol === 'https:' ? 443 : 80)
    const { server, codePromise } = await startLoopback(port, parsed.pathname, state, timeoutMs)
    return { code: codePromise, close: () => server.close() }
  }
})

export async function startSsoLogin(timeoutMs = 5 * 60_000): Promise<IdentityUser> {
  const { clientId } = oidcClient()
  // 授权码 + PKCE:verifier 仅存内存,回环回调换 token 时随 code 一并提交
  const tokens = await ssoFlow.authorize(timeoutMs)
  if (!tokens.id_token || !tokens.access_token) {
    throw new Error('SSO 未返回 id_token/access_token')
  }

  // id_token 验签(JWKS)+ iss/aud 校验;sub 即工号
  const jwks = createRemoteJWKSet(new URL(`${issuer()}/.well-known/jwks.json`))
  const claims = await jwtVerify(tokens.id_token, jwks, { issuer: issuer(), audience: clientId })
  const sub = String(claims.payload.sub ?? '')
  const name = String(claims.payload.name ?? sub)
  if (!sub) throw new Error('id_token 缺少 sub(工号)')

  // userinfo 补充部门(失败不阻断)
  let department = ''
  try {
    const ui = await fetch(`${issuer()}/userinfo`, { headers: { Authorization: `Bearer ${tokens.access_token}` } })
    if (ui.ok) department = String(((await ui.json()) as { dept?: string }).dept ?? '')
  } catch {
    // 部门为可选信息
  }

  // 钉钉 userId（可选 claim，字符串且非空才写入）
  const dingtalkRaw = claims.payload.dingtalk
  const dingtalk = typeof dingtalkRaw === 'string' ? dingtalkRaw.trim() : ''

  // SSO 角色（可选 claim）：用于 agent JIT 账号角色透传，支持数组或逗号分隔字符串
  const rolesRaw = claims.payload.roles
  const roles: string[] = Array.isArray(rolesRaw)
    ? rolesRaw.map((r) => String(r)).filter((r) => r.trim() !== '')
    : typeof rolesRaw === 'string' && rolesRaw.trim() !== ''
      ? rolesRaw.split(',').map((r) => r.trim()).filter((r) => r !== '')
      : []

  const user: IdentityUser = { id: sub, name, department }
  if (roles.length) user.roles = roles
  if (dingtalk) user.dingtalk = dingtalk

  // 先落身份(router token 交换依赖 current),再取 router 凭据
  const identity: Identity = {
    user,
    oidc: {
      idToken: tokens.id_token,
      accessToken: tokens.access_token,
      refreshToken: tokens.refresh_token ?? '',
      expiresAt: Date.now() + (tokens.expires_in ?? 3600) * 1000
    },
    routerKey: '',
    agentJwt: ''
  }
  saveIdentity(identity)

  // router token:失败降级(登录成功但用量/gateway 调用稍后自愈)
  try {
    await freshRouterToken()
  } catch (err) {
    console.warn('[identity] router token 交换降级:', (err as Error).message)
  }

  // agent JIT:失败降级(与既有行为一致)
  try {
    identity.agentJwt = await ensureAgentJwt(sub, name, 'active', roles)
    saveIdentity(identity)
  } catch (err) {
    console.warn('[identity] agent JIT 降级(无 agent JWT):', (err as Error).message)
  }

  return user
}

/** 回环回调服务:校验 state 并交付授权码;固定端口便于 SSO 端精确匹配 */
async function startLoopback(
  port: number,
  path: string,
  state: string,
  timeoutMs: number
): Promise<{ server: Server; codePromise: Promise<string> }> {
  return new Promise((resolve, reject) => {
    let resolveCode: (v: string) => void
    let rejectCode: (e: Error) => void
    const codePromise = new Promise<string>((res, rej) => {
      resolveCode = res
      rejectCode = rej
    })
    const server = createServer((req, res) => {
      const url = new URL(req.url ?? '/', 'http://local')
      const page = (title: string, detail: string): string =>
        '<meta charset="utf-8"><body style="font-family:\'Segoe UI\',\'PingFang SC\',sans-serif;background:#161616;color:#e8e8e8;display:flex;justify-content:center;padding-top:20vh"><div style="text-align:center;max-width:440px;line-height:1.9;padding:0 20px"><h1 style="font-size:20px;margin:0 0 8px">' +
        title +
        '</h1><p style="color:#9b9b9b;font-size:13px;margin:0">' +
        detail +
        '</p></div></body>'
      if (url.pathname === path) {
        if (url.searchParams.get('state') !== state) {
          res
            .writeHead(400, { 'Content-Type': 'text/html; charset=utf-8' })
            .end(page('登录校验失败', '状态参数不匹配,请回到员工端重新发起登录'))
          return
        }
        res
          .writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
          .end(page('✓ 登录成功', '请返回员工 AI 工作台,本页可以直接关闭'))
        resolveCode(url.searchParams.get('code') ?? '')
        return
      }
      // 其它路径(含根路径): 说明本端口用途, 避免"跳到一个空白页"的困惑
      res
        .writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
        .end(
          page(
            '员工端登录回调端口',
            `本地址(127.0.0.1:${port}${path})仅用于接收企业 SSO 回调,请回到员工端发起登录后自动完成。`
          )
        )
    })
    server.once('error', (err: NodeJS.ErrnoException) => {
      if (err.code === 'EADDRINUSE') {
        reject(
          new Error(
            `${port} 端口被占用,无法接收 SSO 回调。请关闭占用该端口的程序(可能是旧版本应用或独立部署的网关)后重试`
          )
        )
      } else {
        reject(err)
      }
    })
    server.listen(port, '127.0.0.1', () => {
      const timer = setTimeout(
        () => rejectCode(new Error(`登录超时(${Math.round(timeoutMs / 60_000)} 分钟未完成)`)),
        timeoutMs
      )
      codePromise.catch(() => {}).finally(() => clearTimeout(timer))
      resolve({ server, codePromise })
    })
  })
}

// ---- LDAP 登录已移除:SSO 是唯一认证入口 ----
