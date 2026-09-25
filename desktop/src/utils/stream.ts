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
