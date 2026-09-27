/** 流式结束的收尾判定(纯逻辑,便于单测) */

export interface StreamFinishState {
  /** 用户主动中止 */
  aborted: boolean
  /** 出错信息(空串=无错) */
  error: string
  /** 最终回答内容 */
  content: string
}

/** 是否需要在流式结束时发「回答完成」通知:中止/出错/无内容都不发 */
export function shouldNotifyStreamFinish(state: StreamFinishState): boolean {
  return !state.aborted && !state.error.trim() && state.content.trim().length > 0
}

export interface StreamInterruptedState {
  /** 用户主动中止 */
  aborted: boolean
  /** 是否到达终态(收到 done / error 事件) */
  terminal: boolean
  /** 已落入消息的错误信息 */
  error: string
}

/**
 * 在线流是否「非预期中断」:既非用户中止, 也未到达终态(error/done), 且当前无错误。
 *
 * 典型场景:SSE 连接被网关/网络掐断, 服务端取消整轮运行, 但客户端既没收到 done
 * 也没收到 error —— 若不做判定, 界面会静默停在半截且无提示(表现为「卡住」)。
 */
export function streamInterrupted(state: StreamInterruptedState): boolean {
  return !state.aborted && !state.terminal && !state.error.trim()
}
