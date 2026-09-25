import { defineStore } from 'pinia'
import { agentApi } from '../api/agent'
import type { LocalSessionMeta, SessionInfo } from '../api/types'

/** 会话列表统一展示形状:agent 与本地会话归一化后的条目 */
export interface SessionListItem {
  id: string
  mode: 'agent' | 'local'
  /** agent 会话无标题,直接用 id 展示 */
  title: string
  createdAt: number
  updatedAt: number
  /** 仅 agent 会话有 */
  messageCount?: number
  isStreaming?: boolean
  /** agent 会话来源渠道:web / dingtalk / other */
  channel?: string
  /** 渠道细类:web / dingtalk / dingtalk_group / local / other(区分钉钉私聊与群) */
  channelKind?: string
  /** 是否置顶(仅在线会话; 离线会话固定 false) */
  pinned?: boolean
  /** 仅本地会话有 */
  workspace?: string
  /** 仅本地会话有: 临时会话(退出后自动删除, 不落历史) */
  ephemeral?: boolean
}

function toMillis(t: string | number): number {
  if (typeof t === 'number') return t
  const parsed = Date.parse(t)
  return Number.isNaN(parsed) ? 0 : parsed
}

/** 归档/导出共用的会话键: `local:<id>` / `agent:<sessionId>` */
export function sessionArchiveKey(mode: 'agent' | 'local', id: string): string {
  return `${mode === 'local' ? 'local' : 'agent'}:${id}`
}

/** 渠道细类(与后端 storage.conversation_kind 对齐): 区分 web / 钉钉私聊 / 钉钉群 */
export type ChannelKind = 'web' | 'dingtalk' | 'dingtalk_group' | 'local' | 'other'

/** 从会话 ID 前缀兜底解析渠道细类(后端 channel_kind 缺失时使用) */
export function channelKindFromId(id: string): ChannelKind {
  const s = String(id || '')
  if (s.startsWith('dingtalk_group:')) return 'dingtalk_group'
  if (s.startsWith('dingtalk:')) return 'dingtalk'
  if (s.startsWith('web:')) return 'web'
  if (s.startsWith('local:')) return 'local'
  return 'other'
}

/** 渠道细类 → 展示文案 */
export function channelLabel(kind: string | undefined): string {
  switch (kind) {
    case 'web':
      return 'Web'
    case 'dingtalk':
      return '钉钉私聊'
    case 'dingtalk_group':
      return '钉钉群'
    case 'local':
      return '本地'
    default:
      return '其他'
  }
}

/** 会话是否可在 dashboard 内续聊: 仅 web(在线)与本地会话; 钉钉会话只读 */
export function canContinueInDashboard(item: { mode: 'agent' | 'local'; id: string; channelKind?: string }): boolean {
  if (item.mode === 'local') return true
  return (item.channelKind || channelKindFromId(item.id)) === 'web'
}

/** agent + 本地会话合并视图,置顶优先、其余按更新时间倒序(纯函数便于单测) */
export function mergeSessionList(agentSessions: SessionInfo[], localSessions: LocalSessionMeta[]): SessionListItem[] {
  const agent: SessionListItem[] = agentSessions.map((s) => ({
    id: s.id,
    mode: 'agent',
    // 展示用标题: 用户重命名优先, 否则去掉 web:{uid}: 前缀的短 id
    title: (s.title ?? '').trim() || s.id.split(':').slice(2).join(':') || s.id,
    pinned: Boolean(s.pinned),
    channel: s.channel,
    channelKind: s.channel_kind || channelKindFromId(s.id),
    createdAt: toMillis(s.first_accessed ?? s.created_at ?? 0),
    updatedAt: toMillis(s.last_accessed ?? s.first_accessed ?? s.created_at ?? 0),
    messageCount: s.messages ?? s.message_count,
    isStreaming: s.is_streaming
  }))
  const local: SessionListItem[] = localSessions.map((s) => ({
    id: s.id,
    mode: 'local',
    title: s.title,
    pinned: false,
    channelKind: 'local',
    createdAt: s.createdAt,
    updatedAt: s.updatedAt,
    workspace: s.workspace,
    ephemeral: s.ephemeral === true
  }))
  return [...agent, ...local].sort((a, b) => {
    if (Boolean(a.pinned) !== Boolean(b.pinned)) return a.pinned ? -1 : 1
    return b.updatedAt - a.updatedAt
  })
}

export const useSessionsStore = defineStore('sessions', {
  state: () => ({
    /** agent 会话原始列表(零号员工) */
    agentSessions: [] as SessionInfo[],
    /** 本地 agent 会话列表(主进程 localagent:sessions:list) */
    localSessions: [] as LocalSessionMeta[],
    /** 归档键集合(主进程 archived.json, 本地与在线通用) */
    archivedKeys: [] as string[],
    loading: false,
    /** 是否成功拉取过一次(用于侧栏显示连接状态) */
    loaded: false
  }),

  getters: {
    /** 全部会话的合并视图(含已归档, 供标题/查找使用) */
    sessions(state): SessionListItem[] {
      return mergeSessionList(state.agentSessions, state.localSessions)
    },

    archivedKeySet(state): Set<string> {
      return new Set(state.archivedKeys)
    },

    /** 主列表(排除已归档) */
    activeSessions(state): SessionListItem[] {
      const archived = new Set(state.archivedKeys)
      return mergeSessionList(state.agentSessions, state.localSessions).filter(
        (s) => !archived.has(sessionArchiveKey(s.mode, s.id))
      )
    },

    /** 已归档会话(侧栏「已归档」分组) */
    archivedSessions(state): SessionListItem[] {
      const archived = new Set(state.archivedKeys)
      return mergeSessionList(state.agentSessions, state.localSessions).filter((s) =>
        archived.has(sessionArchiveKey(s.mode, s.id))
      )
    }
  },

  actions: {
    async refresh(silent = false) {
      this.loading = true
      try {
        // 本地/归档列表失败不阻断 agent 列表(反之亦然),各自静默降级
        const [agentRes, localRes, archiveRes] = await Promise.allSettled([
          agentApi.listSessions(),
          window.desktop.invoke<LocalSessionMeta[]>('localagent:sessions:list'),
          window.desktop.invoke<string[]>('sessions:archive:list')
        ])
        // 先落本地列表,避免 agent 失败分支提前 throw 时本地列表滞留旧数据
        this.localSessions = localRes.status === 'fulfilled' ? (localRes.value ?? []) : []
        this.archivedKeys = archiveRes.status === 'fulfilled' ? (archiveRes.value ?? []) : []
        if (agentRes.status === 'fulfilled') {
          this.agentSessions = agentRes.value.sessions ?? []
          this.loaded = true
        } else {
          this.loaded = false
          if (!silent) throw agentRes.reason
        }
      } finally {
        this.loading = false
      }
    },

    async remove(id: string, mode: 'agent' | 'local' = 'agent') {
      if (mode === 'local') {
        await window.desktop.invoke('localagent:sessions:delete', id)
      } else {
        await agentApi.deleteSession(id)
      }
      // 删除后清理归档键,避免归档集合残留幽灵会话(尽力而为)
      const key = sessionArchiveKey(mode, id)
      if (this.archivedKeys.includes(key)) {
        await this.setArchived(mode, id, false).catch(() => {})
      }
      await this.refresh(true)
    },

    /** 置顶/取消置顶(仅在线会话) */
    async setPinned(id: string, pinned: boolean, mode: 'agent' | 'local' = 'agent') {
      if (mode === 'local') return
      await agentApi.updateSession(id, { pinned })
      await this.refresh(true)
    },

    /** 重命名(本地写回会话 meta, 在线走 agent 接口) */
    async rename(id: string, title: string, mode: 'agent' | 'local' = 'agent') {
      if (mode === 'local') {
        await window.desktop.invoke('localagent:sessions:rename', { id, title })
      } else {
        await agentApi.updateSession(id, { title })
      }
      await this.refresh(true)
    },

    /** 归档/取消归档(主进程 archived.json, 两种模式通用) */
    async setArchived(mode: 'agent' | 'local', id: string, archived: boolean) {
      const keys = await window.desktop.invoke<string[]>('sessions:archive:set', {
        key: sessionArchiveKey(mode, id),
        archived
      })
      this.archivedKeys = keys ?? []
    },

    /** 导出会话(MD/JSON): 主进程读数据源并弹系统保存对话框; title 为渲染层已知名(在线列表未命中时兜底) */
    async exportSession(
      mode: 'agent' | 'local',
      id: string,
      format: 'md' | 'json',
      title?: string
    ): Promise<{ canceled: boolean; path?: string }> {
      return window.desktop.invoke<{ canceled: boolean; path?: string }>('sessions:export', {
        key: sessionArchiveKey(mode, id),
        format,
        title: String(title ?? '').trim() || undefined
      })
    }
  }
})
