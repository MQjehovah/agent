/**
 * 语音输入(E 阶段)的纯逻辑：录音状态机与时长格式化。
 *
 * 状态：idle(空闲) → recording(录音) → transcribing(转写) → idle。
 * 录音中可 cancel 直接回到 idle；转写中同样可 cancel（放弃结果）回 idle；
 * 转写自然结束(done/fail)回到 idle。
 * 事件与状态不匹配时保持当前状态（幂等、可安全重放），避免 UI 竞态把状态打乱。
 */

export type VoiceState = 'idle' | 'recording' | 'transcribing'

export type VoiceEvent = 'start' | 'stop' | 'done' | 'fail' | 'cancel'

/** 未配置 ASR 地址时按钮 tooltip 与提示文案（与主进程 ASR_UNCONFIGURED_ERROR 同文案） */
export const VOICE_UNCONFIGURED_HINT = '未配置语音服务'

/** 状态迁移：非法事件返回原状态 */
export function transitionVoiceState(state: VoiceState, event: VoiceEvent): VoiceState {
  if (state === 'idle') {
    return event === 'start' ? 'recording' : state
  }
  if (state === 'recording') {
    if (event === 'stop') return 'transcribing'
    if (event === 'cancel') return 'idle'
    return state
  }
  // transcribing: 自然结束或用户取消
  if (event === 'done' || event === 'fail' || event === 'cancel') return 'idle'
  return state
}

/** 录音时长格式化为 mm:ss（非法/负数归 0；超过 1 小时继续累加分钟，如 61:01） */
export function formatDuration(ms: number): string {
  const total = Number.isFinite(ms) && ms > 0 ? Math.floor(ms / 1000) : 0
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${p(minutes)}:${p(seconds)}`
}
