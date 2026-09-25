import { findNthUserMessageIndex, lastUserMessageIndex, type LocalSession, type SessionStore } from './session'
import type { ChatMessage } from './types'

/**
 * 会话轮次动作（chat / 重新生成 / 编辑重发）的编排层。
 *
 * 顺序契约：**先同步 claim（同会话并发守卫）→ 再做任何破坏性写**。
 *   守卫命中 → 抛错且存储零改动（不做截断/替换）；
 *   claim 成功后才做异步准备（ensureRouterKey 等，见 execute），失败由本模块 release claim。
 *
 * 不 import electron：store/claim/execute 全部注入，便于离线单测守卫顺序。
 */

/** 占用句柄（与 turn-registry.TurnClaim 同形，避免运行时依赖 electron 类型文件） */
export interface TurnClaimHandle {
  release(): void
}

export interface SessionActionDeps<C extends TurnClaimHandle = TurnClaimHandle> {
  store: Pick<SessionStore, 'getSession' | 'getMessages' | 'truncateMessages' | 'rewriteMessages' | 'buildContext'>
  /** 同步抢占会话：已有活跃流时抛错；必须在破坏性写之前调用 */
  claim(sessionId: string): C
  /**
   * claim 成功后异步执行一轮（身份/router key 准备 + 启动流式循环）。
   * 正常路径由执行方在流结束后结算；抛错时由本模块 release claim。
   */
  execute(
    claim: C,
    session: LocalSession,
    userMessage: string,
    options: { history: ChatMessage[]; skipPersistUserMessage?: boolean }
  ): Promise<{ streamId: string }>
  /** 上下文预算（与 ipc 的 MAX_CONTEXT_CHARS 一致） */
  maxContextChars: number
}

/** 截断后的上下文：buildContext 结果去掉末尾的目标用户消息（由 userMessage 单独传给 loop） */
export function historyBeforeLast(history: ChatMessage[]): ChatMessage[] {
  const last = history[history.length - 1]
  if (!last || last.role !== 'user') throw new Error('消息状态异常,请重试')
  history.pop()
  return history
}

/** 新建对话轮次：守卫（claim）先于 loop 落盘用户消息 */
export async function startLocalTurn<C extends TurnClaimHandle>(
  deps: SessionActionDeps<C>,
  sessionId: string,
  rawMessage: string
): Promise<{ streamId: string }> {
  const session = deps.store.getSession(String(sessionId ?? ''))
  if (!session) throw new Error('会话不存在')
  const message = String(rawMessage ?? '').trim()
  if (!message) throw new Error('消息不能为空')
  // 历史是只读操作，可在 claim 前构建
  const history = deps.store.buildContext(session.id, deps.maxContextChars)
  const claim = deps.claim(session.id)
  try {
    return await deps.execute(claim, session, message, { history })
  } catch (err) {
    claim.release()
    throw err
  }
}

/**
 * 重新生成（C2，仅本地）：claim（守卫）→ 截断到最后一条用户消息（含）→ 复用该消息重跑。
 * 守卫命中时不发生任何截断（修复评审：破坏性写不得先于并发守卫）。
 */
export async function regenerateLocalTurn<C extends TurnClaimHandle>(
  deps: SessionActionDeps<C>,
  sessionId: string
): Promise<{ streamId: string }> {
  const session = deps.store.getSession(String(sessionId ?? ''))
  if (!session) throw new Error('会话不存在')
  const idx = lastUserMessageIndex(deps.store.getMessages(session.id))
  if (idx < 0) throw new Error('没有可重新生成的用户消息')
  const claim = deps.claim(session.id)
  try {
    const kept = deps.store.truncateMessages(session.id, idx + 1)
    const userMessage = kept[kept.length - 1].content
    return await deps.execute(claim, session, userMessage, {
      history: historyBeforeLast(deps.store.buildContext(session.id, deps.maxContextChars)),
      skipPersistUserMessage: true
    })
  } catch (err) {
    claim.release()
    throw err
  }
}

/**
 * 编辑并重发（C3，仅本地）：claim（守卫）→ 替换第 N 条用户消息并截断其后 → 重跑。
 * index 是「第几条用户消息」（存储里夹着 assistant/tool 消息，不能用 UI 数组下标）。
 */
export async function editAndResendLocalTurn<C extends TurnClaimHandle>(
  deps: SessionActionDeps<C>,
  sessionId: string,
  rawIndex: unknown,
  rawText: string
): Promise<{ streamId: string }> {
  const session = deps.store.getSession(String(sessionId ?? ''))
  if (!session) throw new Error('会话不存在')
  const index = Number(rawIndex)
  if (!Number.isInteger(index) || index < 0) throw new Error('消息序号无效')
  const text = String(rawText ?? '').trim()
  if (!text) throw new Error('消息内容不能为空')
  const msgs = deps.store.getMessages(session.id)
  const target = findNthUserMessageIndex(msgs, index)
  if (target < 0) throw new Error('找不到要编辑的用户消息')
  const claim = deps.claim(session.id)
  try {
    const next = msgs.slice(0, target + 1)
    next[target] = { ...next[target], content: text }
    deps.store.rewriteMessages(session.id, next)
    return await deps.execute(claim, session, text, {
      history: historyBeforeLast(deps.store.buildContext(session.id, deps.maxContextChars)),
      skipPersistUserMessage: true
    })
  } catch (err) {
    claim.release()
    throw err
  }
}
