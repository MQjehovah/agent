import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'
import {
  canContinueInDashboard,
  channelKindFromId,
  channelLabel,
  mergeSessionList,
  sessionArchiveKey,
  useSessionsStore
} from '../../src/stores/sessions'

/**
 * 会话组织(D)渲染层:归档键/分组 getter 与会话操作通道契约。
 */

test('sessions: activeSessions/archivedSessions 按归档键分组', () => {
  setActivePinia(createPinia())
  const store = useSessionsStore()
  store.agentSessions = [
    {
      id: 'web:42:a',
      title: '在线A',
      pinned: 0,
      first_accessed: '2023-11-14T22:00:00Z',
      last_accessed: '2023-11-14T22:10:00Z'
    }
  ]
  store.localSessions = [
    { id: 'local-00000001', mode: 'local', title: '本地A', model: 'm', workspace: 'w', createdAt: 100, updatedAt: 300 },
    {
      id: 'local-00000002',
      mode: 'local',
      title: '临时B',
      model: 'm',
      workspace: 'w',
      createdAt: 200,
      updatedAt: 400,
      ephemeral: true
    }
  ]
  store.archivedKeys = ['local:local-00000001']

  // 全量视图: 置顶优先, 其余更新时间倒序
  assert.deepEqual(store.sessions.map((s) => s.title), ['在线A', '临时B', '本地A'])
  // 主列表排除已归档,归档分组单独返回
  assert.deepEqual(store.activeSessions.map((s) => s.title), ['在线A', '临时B'])
  assert.deepEqual(store.archivedSessions.map((s) => s.title), ['本地A'])
  // 临时会话标记透传
  assert.equal(store.sessions.find((s) => s.id === 'local-00000002')?.ephemeral, true)

  assert.equal(sessionArchiveKey('local', 'local-00000001'), 'local:local-00000001')
  assert.equal(sessionArchiveKey('agent', 'web:42:a'), 'agent:web:42:a')
})

test('channels: 渠道细类/文案 + dashboard 续聊判定(方案A)', () => {
  assert.equal(channelKindFromId('web:7:a'), 'web')
  assert.equal(channelKindFromId('dingtalk:7:a'), 'dingtalk')
  assert.equal(channelKindFromId('dingtalk_group:cid:rand'), 'dingtalk_group')
  assert.equal(channelKindFromId('wecom:7:a'), 'other')
  assert.equal(channelLabel('web'), 'Web')
  assert.equal(channelLabel('dingtalk'), '钉钉私聊')
  assert.equal(channelLabel('dingtalk_group'), '钉钉群')

  // 仅 web 在线会话与本地会话可在 dashboard 续聊; 钉钉私聊/群只读
  assert.equal(canContinueInDashboard({ mode: 'agent', id: 'web:7:a' }), true)
  assert.equal(canContinueInDashboard({ mode: 'agent', id: 'dingtalk:7:a' }), false)
  assert.equal(canContinueInDashboard({ mode: 'agent', id: 'dingtalk_group:x:y' }), false)
  assert.equal(canContinueInDashboard({ mode: 'local', id: 'local-1' }), true)
})

test('channels: mergeSessionList 透传 channelKind(服务端优先, 缺失按前缀兜底)', () => {
  const merged = mergeSessionList(
    [
      { id: 'web:1:a', channel: 'web', channel_kind: 'web' },
      { id: 'dingtalk:1:b', channel: 'dingtalk', channel_kind: 'dingtalk' },
      { id: 'dingtalk_group:c:r', channel: 'dingtalk' } // 无 channel_kind → 前缀兜底
    ],
    []
  )
  const byId = Object.fromEntries(merged.map((s) => [s.id, s.channelKind]))
  assert.equal(byId['web:1:a'], 'web')
  assert.equal(byId['dingtalk:1:b'], 'dingtalk')
  assert.equal(byId['dingtalk_group:c:r'], 'dingtalk_group')
})

test('sessions.setArchived/exportSession: 通道与载荷契约', async () => {
  setActivePinia(createPinia())
  const store = useSessionsStore()
  const calls: Array<{ channel: string; payload: unknown }> = []
  Object.assign(globalThis, {
    window: {
      desktop: {
        invoke: async (channel: string, payload?: unknown) => {
          calls.push({ channel, payload })
          if (channel === 'sessions:archive:set') return ['local:local-00000001']
          return { canceled: false, path: 'C:/out/x.md' }
        }
      }
    }
  })

  await store.setArchived('local', 'local-00000001', true)
  assert.deepEqual(calls[0], {
    channel: 'sessions:archive:set',
    payload: { key: 'local:local-00000001', archived: true }
  })
  assert.deepEqual(store.archivedKeys, ['local:local-00000001'])

  const res = await store.exportSession('agent', 'web:42:a', 'md', '在线A')
  assert.deepEqual(calls[1], {
    channel: 'sessions:export',
    // 渲染层已知标题随请求带上: 主进程列表未命中(200 条上限外)时兜底
    payload: { key: 'agent:web:42:a', format: 'md', title: '在线A' }
  })
  assert.equal(res.path, 'C:/out/x.md')
})
