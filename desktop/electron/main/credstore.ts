import { createCipheriv, createDecipheriv, randomBytes, scryptSync } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * 网关侧凭据存储:JIT 开通的 agent 账号的一次性密码(AES-256-GCM 加密落盘)。
 * 设计:每个 SSO 身份首次登录时在 agent 侧建号并写入随机密码,网关加密保存;
 * 后续登录复用该密码向 agent 换 JWT;仅当密码被外部改动导致登录失败时才重置一次。
 */
function dataDir(): string {
  return process.env.GATEWAY_DATA_DIR ?? join(process.cwd(), 'data')
}

let keyCache: Buffer | null = null

function getKey(): Buffer {
  if (!keyCache) {
    const keyPath = join(dataDir(), 'secret.key')
    if (existsSync(keyPath)) {
      keyCache = readFileSync(keyPath)
    } else {
      mkdirSync(dataDir(), { recursive: true })
      keyCache = scryptSync(randomBytes(32).toString('hex'), 'gateway-credential-key-v1', 32)
      writeFileSync(keyPath, keyCache, { mode: 0o600 })
    }
  }
  return keyCache
}

export function encrypt(text: string): string {
  const iv = randomBytes(12)
  const cipher = createCipheriv('aes-256-gcm', getKey(), iv)
  const enc = Buffer.concat([cipher.update(text, 'utf-8'), cipher.final()])
  return [iv.toString('base64'), cipher.getAuthTag().toString('base64'), enc.toString('base64')].join('.')
}

export function decrypt(payload: string): string {
  const [ivB64, tagB64, dataB64] = payload.split('.')
  const decipher = createDecipheriv('aes-256-gcm', getKey(), Buffer.from(ivB64, 'base64'))
  decipher.setAuthTag(Buffer.from(tagB64, 'base64'))
  return Buffer.concat([decipher.update(Buffer.from(dataB64, 'base64')), decipher.final()]).toString('utf-8')
}

export interface StoredCred {
  agentUserId: number | string
  agentPassword: string
}

function credsFile(): string {
  return join(dataDir(), 'agent-credentials.json')
}

function readAll(): Record<string, StoredCred> {
  if (!existsSync(credsFile())) return {}
  try {
    const raw = JSON.parse(readFileSync(credsFile(), 'utf-8')) as Record<string, { agentUserId: number | string; agentPasswordEnc: string }>
    const out: Record<string, StoredCred> = {}
    for (const [sub, v] of Object.entries(raw)) {
      try {
        out[sub] = { agentUserId: v.agentUserId, agentPassword: decrypt(v.agentPasswordEnc) }
      } catch {
        // 解密失败(密钥更换等)→ 视为无凭据,走重置流程
      }
    }
    return out
  } catch {
    return {}
  }
}

function writeAll(all: Record<string, StoredCred>): void {
  mkdirSync(dataDir(), { recursive: true })
  const raw: Record<string, { agentUserId: number | string; agentPasswordEnc: string }> = {}
  for (const [sub, v] of Object.entries(all)) {
    raw[sub] = { agentUserId: v.agentUserId, agentPasswordEnc: encrypt(v.agentPassword) }
  }
  writeFileSync(credsFile(), JSON.stringify(raw, null, 2), 'utf-8')
}

export function getCred(sub: string): StoredCred | null {
  return readAll()[sub] ?? null
}

export function saveCred(sub: string, cred: StoredCred): void {
  const all = readAll()
  all[sub] = cred
  writeAll(all)
}
