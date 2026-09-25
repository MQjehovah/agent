import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  formatDuration,
  transitionVoiceState,
  VOICE_UNCONFIGURED_HINT,
  type VoiceEvent,
  type VoiceState
} from '../../src/utils/voice'

/** 语音输入(E)的状态机与时长格式化纯逻辑 */

test('voice: idle -> recording -> transcribing -> idle 主链路', () => {
  assert.equal(transitionVoiceState('idle', 'start'), 'recording')
  assert.equal(transitionVoiceState('recording', 'stop'), 'transcribing')
  assert.equal(transitionVoiceState('transcribing', 'done'), 'idle')
  assert.equal(transitionVoiceState('transcribing', 'fail'), 'idle')
})

test('voice: 录音中取消直接回 idle, 不进入转写', () => {
  assert.equal(transitionVoiceState('recording', 'cancel'), 'idle')
})

test('voice: 转写中可取消(放弃结果)回 idle', () => {
  assert.equal(transitionVoiceState('transcribing', 'cancel'), 'idle')
})

test('voice: 不匹配事件保持原状态(幂等, 防 UI 竞态)', () => {
  const states: VoiceState[] = ['idle', 'recording', 'transcribing']
  const events: VoiceEvent[] = ['start', 'stop', 'done', 'fail', 'cancel']
  const legal: Record<VoiceState, VoiceEvent[]> = {
    idle: ['start'],
    recording: ['stop', 'cancel'],
    transcribing: ['done', 'fail', 'cancel']
  }
  for (const state of states) {
    for (const event of events) {
      const next = transitionVoiceState(state, event)
      if (legal[state].includes(event)) {
        assert.notEqual(next, state, `${state} + ${event} 必有迁移`)
      } else {
        assert.equal(next, state, `${state} + ${event} 应保持原状态`)
      }
    }
  }
  // 重复 stop / 重复 done 幂等
  assert.equal(transitionVoiceState('transcribing', 'stop'), 'transcribing')
  assert.equal(transitionVoiceState('idle', 'done'), 'idle')
})

test('voice: 时长格式化为 mm:ss', () => {
  assert.equal(formatDuration(0), '00:00')
  assert.equal(formatDuration(999), '00:00')
  assert.equal(formatDuration(1000), '00:01')
  assert.equal(formatDuration(65_000), '01:05')
  assert.equal(formatDuration(3_661_000), '61:01')
})

test('voice: 时长非法值归零', () => {
  assert.equal(formatDuration(-1), '00:00')
  assert.equal(formatDuration(Number.NaN), '00:00')
})

test('voice: 未配置提示文案与主进程一致', () => {
  assert.equal(VOICE_UNCONFIGURED_HINT, '未配置语音服务')
})
