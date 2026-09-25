import { randomBytes } from 'node:crypto'
import type { WebContents } from 'electron'

/**
 * 本地会话轮次占用登记：同会话并发守卫 + stream/sender 账本。
 *
 * claim 是**同步**函数，必须在任何破坏性写（截断/改写消息、落盘用户消息）之前调用：
 *   守卫命中 → 抛中文错误且存储零改动；成功 → 登记 streams/senders 并挂窗口销毁清理。
 * 失败路径用 handle.release() 释放（幂等），保证不残留 streams/senders 与监听器。
 *
 * 仅 `import type` 依赖 electron（运行时擦除），便于离线单测。
 */

export interface TurnStream {
  controller: AbortController
  sessionId: string
}

/** 占用句柄：正常路径由流结束后的 settle 结算；失败路径 release */
export interface TurnClaim {
  readonly sessionId: string
  readonly streamId: string
  readonly controller: AbortController
  /** 事件/权限请求的目标窗口 */
  readonly sender: WebContents
  /** 窗口销毁清理回调（正常结束时由执行方摘除监听） */
  readonly onSenderDestroyed: () => void
  release(): void
}

export interface TurnRegistry {
  /** 只读账本（停止指令/权限请求路由/诊断用） */
  readonly streams: Map<string, TurnStream>
  readonly senders: Map<string, WebContents>
  hasActive(sessionId: string): boolean
  streamIdOf(sessionId: string): string | undefined
  senderOf(sessionId: string): WebContents | null
  /** 同步占用会话；已有活跃流时抛错（不产生任何副作用） */
  claim(sessionId: string, sender: WebContents): TurnClaim
  /** 流正常结束的结算：删流；该会话无活跃流时清 sender 并回调 onIdle */
  settle(streamId: string, sessionId: string, onIdle?: (sessionId: string) => void): void
}

export interface TurnRegistryOptions {
  /** 窗口销毁时的额外清理（结算未决权限等）；账本清理由本模块负责 */
  onSenderDestroyed?: (sessionId: string) => void
  /** 测试可注入确定性 streamId 生成器 */
  newStreamId?: () => string
}

export function createTurnRegistry(options: TurnRegistryOptions = {}): TurnRegistry {
  const streams = new Map<string, TurnStream>()
  const senders = new Map<string, WebContents>()
  const newStreamId = options.newStreamId ?? (() => randomBytes(8).toString('hex'))

  const hasActive = (sessionId: string): boolean =>
    [...streams.values()].some((s) => s.sessionId === sessionId)

  return {
    streams,
    senders,
    hasActive,

    streamIdOf(sessionId) {
      return [...streams.entries()].find(([, s]) => s.sessionId === sessionId)?.[0]
    },

    senderOf(sessionId) {
      const sender = senders.get(sessionId)
      if (!sender) return null
      if (sender.isDestroyed()) {
        senders.delete(sessionId)
        return null
      }
      return sender
    },

    claim(sessionId, sender) {
      // 同会话并发守卫：已有活跃流时拒绝，且不做任何登记（存储层零改动由调用顺序保证）
      if (hasActive(sessionId)) {
        throw new Error('该会话正在对话中，请稍候或停止后重试')
      }
      const streamId = newStreamId()
      const controller = new AbortController()
      streams.set(streamId, { controller, sessionId })
      senders.set(sessionId, sender)

      // sender（窗口）销毁中途清理：abort 该会话全部活跃流 + 清账本 + 交回调做业务清理。
      // 与 settle/release 幂等共存（Map.delete 重复调用安全）。
      const onSenderDestroyed = (): void => {
        for (const [id, s] of streams) {
          if (s.sessionId !== sessionId) continue
          s.controller.abort()
          streams.delete(id)
        }
        senders.delete(sessionId)
        options.onSenderDestroyed?.(sessionId)
      }
      sender.once('destroyed', onSenderDestroyed)

      let released = false
      return {
        sessionId,
        streamId,
        controller,
        sender,
        onSenderDestroyed,
        release() {
          if (released) return
          released = true
          streams.delete(streamId)
          controller.abort()
          if (!sender.isDestroyed()) sender.removeListener('destroyed', onSenderDestroyed)
          if (!hasActive(sessionId)) senders.delete(sessionId)
        }
      }
    },

    settle(streamId, sessionId, onIdle) {
      streams.delete(streamId)
      if (hasActive(sessionId)) return
      senders.delete(sessionId)
      onIdle?.(sessionId)
    }
  }
}
