import { randomBytes } from 'node:crypto'

/**
 * 权限网关：write 类工具执行前经 UI 用户审批。
 * 单一职责：只管理请求的挂起/裁决/会话级放行，不判断 read/write——
 * 是否需要询问由调用方（loop，据 registry 的 tool.kind）决定。
 */

/** 权限请求通知负载，经 IPC emit 到渲染端 */
export interface PermissionRequest {
  requestId: string
  sessionId: string
  tool: string
  summary: string
}

/** 用户裁决：allow 仅本次，deny 拒绝，allow_session 本会话内该工具永久放行，
 *  allow_always 跨会话/重启永久免问（写入权限记忆，见 permission-memory.ts） */
export type PermissionDecision = 'allow' | 'deny' | 'allow_session' | 'allow_always'

/** 权限记忆注入接口：has=是否已记住（免问），add=记住该工具（落盘由实现方负责） */
export interface PermissionMemory {
  has(tool: string): boolean
  add(tool: string): void
}

/**
 * 本地权限模式(与在线三档对齐):
 *   default 每次询问 —— 所有 write 类工具都确认
 *   smart   必要时询问 —— terminal 与 MCP 写工具确认；内建文件写（file_write/file_edit，限定工作区内）降噪不问
 *   auto    完全访问 —— 不再确认
 */
export type LocalPermissionMode = 'default' | 'smart' | 'auto'

/** smart 模式下需要确认的高危工具（本地内核执行终端命令的工具名是 terminal） */
const DANGEROUS_TOOLS = new Set(['terminal'])

/** MCP 工具名前缀（mcp.ts mcpToolName 的约定）：外部副作用工具，smart 下写操作需确认 */
const MCP_TOOL_PREFIX = 'mcp__'

export interface PermissionGateway {
  /** 请求授权：无需询问(权限记忆/会话级放行/模式判定)直接 true，否则挂起等待 respond 裁决 */
  ask(sessionId: string, tool: string, summary: string): Promise<boolean>
  /** 按 requestId 裁决；未知 id 安全 no-op；allow_always 会写入权限记忆（跨会话永久免问） */
  respond(requestId: string, decision: PermissionDecision): void
  /** 取消会话全部未决请求（以 false resolve，避免 loop 协程永久挂起）；clearGrants 时一并清除该会话级放行 */
  cancel(sessionId: string, clearGrants?: boolean): void
  getMode(): LocalPermissionMode
  setMode(mode: LocalPermissionMode): void
  /**
   * 当前模式下该工具是否需要弹窗确认。
   * kind 供 smart 判定 MCP 工具读写：MCP 写问、MCP 读不问；terminal 无论 kind 都问；
   * 内建 file_write/file_edit 与 market 远程工具不在 smart 询问范围（default 仍由调用方按 kind 把关）。
   */
  needsAsk(tool: string, kind?: 'read' | 'write'): boolean
}

/** 待裁决请求：resolve 句柄 + 归属信息（归属信息当前仅存档，便于调试/扩展） */
interface PendingEntry {
  resolve: (v: boolean) => void
  sessionId: string
  tool: string
}

export function createPermissions(
  notify: (req: PermissionRequest) => void,
  memory?: PermissionMemory
): PermissionGateway {
  /** requestId → 待裁决请求 */
  const pending = new Map<string, PendingEntry>()
  /** 会话级放行集合，键为 `${sessionId}:${tool}`，进程生命周期内有效 */
  const sessionGranted = new Set<string>()
  /** 当前权限模式(由界面设置, 与在线三档同名) */
  let mode: LocalPermissionMode = 'default'

  return {
    getMode() {
      return mode
    },

    setMode(next) {
      if (next === 'default' || next === 'smart' || next === 'auto') mode = next
    },

    needsAsk(tool, kind) {
      // 已记住的工具（跨会话永久免问）优先于模式判定
      if (memory?.has(tool)) return false
      if (mode === 'auto') return false
      if (mode === 'smart') {
        if (DANGEROUS_TOOLS.has(tool)) return true
        return tool.startsWith(MCP_TOOL_PREFIX) && kind === 'write'
      }
      return true
    },

    ask(sessionId, tool, summary) {
      // 双保险：needsAsk 已滤过，调用方若绕过仍不弹窗（含已记住工具的持久放行）
      if (memory?.has(tool)) return Promise.resolve(true)
      const key = `${sessionId}:${tool}`
      // 已有会话级放行：直接放行，不再打扰 UI
      if (sessionGranted.has(key)) return Promise.resolve(true)
      const requestId = randomBytes(8).toString('hex')
      const promise = new Promise<boolean>((resolve) => {
        pending.set(requestId, { resolve, sessionId, tool })
      })
      // notify 同步调用；UI 侧异常不能破坏 ask 的挂起语义
      try {
        notify({ requestId, sessionId, tool, summary })
      } catch {
        // 吞掉通知失败，请求保持未决，等待用户经 IPC respond
      }
      return promise
    },

    respond(requestId, decision) {
      const entry = pending.get(requestId)
      // 未知/已裁决的 requestId：安全 no-op，不抛错不复活
      if (!entry) return
      pending.delete(requestId)
      if (decision === 'allow_session') {
        sessionGranted.add(`${entry.sessionId}:${entry.tool}`)
      } else if (decision === 'allow_always') {
        // 记住失败（如磁盘异常）不阻断本次放行，仅告警
        try {
          memory?.add(entry.tool)
        } catch (err) {
          console.warn(`[kernel] 权限记忆写入失败: ${err instanceof Error ? err.message : String(err)}`)
        }
      }
      entry.resolve(decision !== 'deny')
    },

    cancel(sessionId, clearGrants = false) {
      // 以 false 结算该会话全部未决请求并从 Map 删除，避免调用方协程永久挂起
      for (const [requestId, entry] of pending) {
        if (entry.sessionId !== sessionId) continue
        pending.delete(requestId)
        entry.resolve(false)
      }
      // 保守默认：仅清理挂起；clearGrants 为 true 时才清除该会话全部放行条目
      if (clearGrants) {
        for (const key of sessionGranted) {
          if (key.startsWith(`${sessionId}:`)) sessionGranted.delete(key)
        }
      }
    },
  }
}
