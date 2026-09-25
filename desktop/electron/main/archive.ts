import { randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'

/**
 * 会话归档(D1): 本地维护归档键集合, 落盘 `<gateway data dir>/archived.json`。
 * 键形如 `local:local-abcd1234` / `agent:web:42:deadbeef`(模式前缀 + 会话 id),
 * 本地与在线会话通用; 归档不删除消息, 可随时取消归档。
 * 与 session store 同约定: 直接读写文件、原子写(临时文件 + rename)、损坏容错。
 */

export interface ArchiveStore {
  list(): string[]
  has(key: string): boolean
  /** 归档(archived=true, 幂等去重)或取消归档(false, no-op 幂等); 返回最新集合 */
  set(key: string, archived: boolean): string[]
}

const KEY_MAX_LEN = 300

/** 归档键归一/校验: 去空白后必须形如 `<mode>:<id>` 且不含控制字符; 非法返回空串 */
export function normalizeArchiveKey(key: unknown): string {
  const k = String(key ?? '').trim()
  if (!k || k.length > KEY_MAX_LEN) return ''
  if (/[\u0000-\u001f\u007f]/.test(k)) return ''
  if (!/^[a-z]+:.+$/.test(k)) return ''
  return k
}

/** 读集合: 文件缺失/损坏/非数组一律按空集合, 过滤非法与重复条目 */
function readKeys(file: string): string[] {
  try {
    if (!existsSync(file)) return []
    const raw: unknown = JSON.parse(readFileSync(file, 'utf-8'))
    if (!Array.isArray(raw)) return []
    const out: string[] = []
    for (const item of raw) {
      const key = normalizeArchiveKey(item)
      if (key && !out.includes(key)) out.push(key)
    }
    return out
  } catch {
    return []
  }
}

/** 原子写: 先写临时文件再 rename, 避免半截文件把归档集合写坏 */
function writeKeys(file: string, keys: string[]): void {
  mkdirSync(dirname(file), { recursive: true })
  const tmp = `${file}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`
  writeFileSync(tmp, `${JSON.stringify(keys, null, 2)}\n`, 'utf-8')
  try {
    renameSync(tmp, file)
  } catch (err) {
    rmSync(tmp, { force: true })
    throw err
  }
}

export function createArchiveStore(file: string): ArchiveStore {
  return {
    list() {
      return readKeys(file)
    },

    has(key) {
      const k = normalizeArchiveKey(key)
      return k ? readKeys(file).includes(k) : false
    },

    set(key, archived) {
      const k = normalizeArchiveKey(key)
      if (!k) throw new Error('归档键无效')
      const keys = readKeys(file)
      const exists = keys.includes(k)
      if (archived && !exists) keys.push(k)
      if (!archived && exists) keys.splice(keys.indexOf(k), 1)
      // 状态未变化时不重写文件, 保持 no-op 幂等
      if (archived !== exists) writeKeys(file, keys)
      return keys
    }
  }
}
