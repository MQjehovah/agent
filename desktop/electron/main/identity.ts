import { createServer, type Server } from 'node:http'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { app, shell } from 'electron'
import { createRemoteJWKSet, jwtVerify } from 'jose'
import { getConfig } from './store'
import { resolveOidcIssuer, resolveOidcClientId, resolveOidcClientSecret } from './oidc-config'
import { createSsoFlow } from './sso-flow'
import { ensureAgentJwt, agentBase } from './agent-jit'
import { encrypt, decrypt } from './credstore'
import { createGatewayCredentials } from './gateway-credentials'

/**
 * 身份与凭据管理(仅主进程):
 *   - OIDC SSO 登录(系统浏览器 + loopback 回调),持有 id/access/refresh token
 *   - gateway access_token:用 id_token 走 RFC 8693 token-exchange 换取(供 /api/me/* 与 rag/market 直连等调用)
 *   - gateway apikey:用 gateway access_token 调 gateway admin /api/me/key 自取(sk-,供 gateway/agent)
 *   - agent JWT:按工号 JIT 开号换取
 *   - rag/market 用 gateway 受众平台 token(id_token 经 RFC 8693 换取,不再复用 OIDC access_token);agent 用 agent JWT;gateway 用 apikey
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
  /** gateway apikey(sk-),供 gateway 调用与本地 agent 使用 */
  gatewayKey: string
  /** gateway 受众平台 token(id_token 换取):用于 /api/me/* 与 rag/market 直连(upstream/media/kb_search/mcp) */
  gatewayToken?: string
  /** gatewayToken 过期时间戳(ms) */
  gatewayTokenExpiresAt?: number
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
  // SSO refresh_token 单次使用轮换: 刷新成功立即把新代理 token 复投给 agent(失败仅告警)
  await hostSsoTokens()
}

/**
 * 取当前可用的 OIDC access_token:临近过期(60s 内)自动用 refresh_token 续期。
 * 保留原因:本地会话的 SSO 续期/轮换检测(kernel/ipc.ts)仍用它触发刷新;
 * rag/market 等外发点(upstream/media/kb_search/market)均已改用 gateway 受众平台 token。
 * 刷新失败不抛异常(返回现有 token 或 null),由调用方按 401/降级自行处理。
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
      identity.user.roles ?? [],
      identity.user.department ?? ''
    )
    if (current) {
      current.agentJwt = next
      saveIdentity(current)
    }
    await hostSsoTokens()
    return next
  } catch (err) {
    console.warn('[identity] agent JWT 续期失败:', (err as Error).message)
    return jwt || null
  }
}

/**
 * 把 SSO 的 id_token/refresh_token 托管给 agent(供服务端按用户 OBO 交换, 幂等覆盖)。
 * SSO refresh_token 为单次使用轮换: 约定「桌面复投为主、agent 侧仅兜底刷新」,
 * 故登录成功、agent JWT 续期、OIDC 刷新后都复投, 保持 agent 副本新鲜。
 * 需 agent JWT 就绪; 失败仅告警, 不阻断登录/续期。
 */
async function hostSsoTokens(): Promise<void> {
  const identity = current
  const oidc = identity?.oidc
  if (!identity?.agentJwt || !oidc?.idToken) return
  try {
    const res = await fetch(`${agentBase()}/api/auth/sso-tokens`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${identity.agentJwt}`
      },
      body: JSON.stringify({
        id_token: oidc.idToken,
        refresh_token: oidc.refreshToken,
        // token 来源客户端(桌面 SSO 登录所用 client), agent 侧刷新/交换按它执行
        client_id: oidcClient().clientId
      }),
      signal: AbortSignal.timeout(10_000)
    })
    if (!res.ok) console.warn(`[identity] SSO token 托管失败(HTTP ${res.status})`)
  } catch (err) {
    console.warn('[identity] SSO token 托管失败:', (err as Error).message)
  }
}

// ---- gateway 凭据(token 交换 + apikey 自取) ----

const gatewayCredentials = createGatewayCredentials<Identity>({
  getIdentity: () => current,
  saveIdentity,
  ensureFreshOidc,
  issuer,
  oidcClient,
  gatewayAdminUrl: () => getConfig().gatewayAdminUrl
})

/**
 * 取新鲜 gateway token:未过期(>60s 余量)直接返回;过期/缺失时交换;并发调用合并为一次交换(单飞);
 * 交换失败但旧 token 未过期时回退旧值,否则抛错。
 */
export function freshGatewayToken(): Promise<string> {
  return gatewayCredentials.freshGatewayToken()
}

/** 401 自愈:清零 token 过期时间并落盘后强制重换(usage 首次 401 时重试用) */
export function renewGatewayToken(): Promise<string> {
  return gatewayCredentials.renewGatewayToken()
}

/** 取 gateway apikey:已有则直接返回;否则调 gateway admin /api/me/key(401 时重换 token 重试一次) */
export function fetchGatewayKey(): Promise<string> {
  return gatewayCredentials.fetchGatewayKey()
}

/** 兼容既有调用点:语义等同 fetchGatewayKey() */
export function ensureGatewayKey(): Promise<string> {
  return fetchGatewayKey()
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

  // 先落身份(gateway token 交换依赖 current),再取 gateway 凭据
  const identity: Identity = {
    user,
    oidc: {
      idToken: tokens.id_token,
      accessToken: tokens.access_token,
      refreshToken: tokens.refresh_token ?? '',
      expiresAt: Date.now() + (tokens.expires_in ?? 3600) * 1000
    },
    gatewayKey: '',
    agentJwt: ''
  }
  saveIdentity(identity)

  // gateway token:失败降级(登录成功但用量/gateway 调用稍后自愈)
  try {
    await freshGatewayToken()
  } catch (err) {
    console.warn('[identity] gateway token 交换降级:', (err as Error).message)
  }

  // agent JIT:失败降级(与既有行为一致)
  try {
    identity.agentJwt = await ensureAgentJwt(sub, name, 'active', roles, user.department ?? '')
    saveIdentity(identity)
    // 登录成功即托管 SSO token(失败仅告警): 桌面用户从此可走 agent OBO 交换
    await hostSsoTokens()
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
