/**
 * 选文件令牌存储：渲染层只拿到主进程签发的一次性 token，
 * 真正的源路径留在主进程内存里，`attach:import` 凭 token 取路径。
 *
 * 纯逻辑（无 Electron / 无 fs 依赖），便于单测锁定「未授权 / 过期 / 单次消费」边界。
 */

export interface PickedPathsEntry {
  paths: string[]
  expiresAt: number
}

/** 令牌存储：token → 选中路径 + 过期时间 */
export type PickedPathsStore = Map<string, PickedPathsEntry>

/** 令牌有效期：10 分钟，足够用户在聊天的选择/发送之间周转 */
export const PICK_TOKEN_TTL_MS = 10 * 60 * 1000

/** 写入一条选中路径记录（token 由调用方用随机数生成） */
export function storePickedPaths(
  store: PickedPathsStore,
  token: string,
  paths: string[],
  now: number = Date.now(),
  ttl: number = PICK_TOKEN_TTL_MS
): void {
  store.set(token, { paths, expiresAt: now + ttl })
}

/** 清理全部过期条目；每次入口调用一次，避免过期记录长期驻留内存 */
export function prunePickedPaths(store: PickedPathsStore, now: number = Date.now()): void {
  for (const [token, entry] of store) {
    if (entry.expiresAt <= now) store.delete(token)
  }
}

/**
 * 解析 token 返回源路径；未知或过期都抛中文错误。
 * 本函数不做删除——由调用方在导入成功后调用 consumePickedPaths 保证单次消费。
 */
export function resolvePickedPaths(
  store: PickedPathsStore,
  token: string,
  now: number = Date.now()
): string[] {
  prunePickedPaths(store, now)
  const entry = store.get(token)
  if (!entry || entry.expiresAt <= now) {
    throw new Error('附件选择已失效或未授权，请重新选择文件')
  }
  return entry.paths
}

/** 消费 token：导入成功后删除，使同一个 token 无法二次导入 */
export function consumePickedPaths(store: PickedPathsStore, token: string): void {
  store.delete(token)
}
