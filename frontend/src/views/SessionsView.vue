<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, del } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

interface SessionRow {
  id: string
  created_at?: string
  first_accessed?: string
  last_accessed?: string
  message_count?: number
  messages?: number
  is_streaming?: boolean
  channel?: string
  source?: 'live' | 'history'
}
interface HistoryMsg { role: string; content: string }

const sessions = ref<SessionRow[]>([])
const loading = ref(false)
const viewVisible = ref(false)
const viewId = ref('')
const viewMessages = ref<HistoryMsg[]>([])

function channelMeta(ch?: string): { label: string; type: 'primary' | 'success' | 'info' | 'warning' } {
  switch (ch) {
    case 'web': return { label: 'Web', type: 'primary' }
    case 'dingtalk': return { label: '钉钉', type: 'success' }
    case 'feishu': return { label: '飞书', type: 'warning' }
    default: return { label: ch || '—', type: 'info' }
  }
}

async function load() {
  loading.value = true
  try {
    const [live, hist] = await Promise.all([
      api<{ sessions: SessionRow[] }>('/api/sessions').catch(() => ({ sessions: [] as SessionRow[] })),
      api<{ sessions: SessionRow[] }>('/api/agent/sessions/history?limit=200').catch(() => ({ sessions: [] as SessionRow[] }))
    ])
    const map = new Map<string, SessionRow>()
    for (const s of hist.sessions ?? []) {
      if (s.id) {
        map.set(s.id, {
          id: s.id,
          created_at: s.first_accessed,
          last_accessed: s.last_accessed,
          message_count: s.messages,
          channel: s.channel,
          source: 'history'
        } as SessionRow)
      }
    }
    for (const s of live.sessions ?? []) {
      if (s.id) {
        const prev = map.get(s.id)
        map.set(s.id, { ...s, channel: s.channel || prev?.channel, source: 'live' })
      }
    }
    sessions.value = Array.from(map.values()).sort((a, b) =>
      (b.last_accessed || b.created_at || '').localeCompare(a.last_accessed || a.created_at || ''))
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

async function view(row: SessionRow) {
  viewId.value = row.id
  viewVisible.value = true
  viewMessages.value = []
  let msgs: HistoryMsg[] = []
  try {
    const d = await api<{ messages: HistoryMsg[] }>(`/api/sessions/${encodeURIComponent(row.id)}/messages`)
    msgs = d.messages ?? []
  } catch {
    /* 内存会话不存在则回退 DB 历史 */
    try {
      const d = await api<{ messages: HistoryMsg[] }>(`/api/agent/sessions/messages?session_id=${encodeURIComponent(row.id)}`)
      msgs = d.messages ?? []
    } catch (e) {
      ElMessage.error((e as Error).message)
      return
    }
  }
  viewMessages.value = msgs
}

async function remove(row: SessionRow) {
  try {
    await ElMessageBox.confirm(`删除会话 ${row.id}?`, '确认', { type: 'warning' })
  } catch { return }
  try {
    await del(`/api/sessions/${encodeURIComponent(row.id)}`)
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div><h2>会话</h2><div class="sub">我的历史对话记录(含已落盘内容)</div></div>
      <el-button @click="load">刷新</el-button>
    </div>
    <el-table :data="sessions" v-loading="loading" empty-text="暂无会话">
      <el-table-column prop="id" label="会话 ID" min-width="200" show-overflow-tooltip class-name="mono" />
      <el-table-column label="来源" width="90" align="center">
        <template #default="{ row }">
          <el-tag :type="row.source === 'live' ? 'warning' : 'info'" size="small">
            {{ row.source === 'live' ? '活跃' : '历史' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="渠道" width="90" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.channel" :type="channelMeta(row.channel).type" size="small">
            {{ channelMeta(row.channel).label }}
          </el-tag>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column label="最近活跃" width="180">
        <template #default="{ row }">
          {{ new Date(row.last_accessed || row.created_at).toLocaleString() }}
        </template>
      </el-table-column>
      <el-table-column prop="message_count" label="消息数" width="90" align="center" />
      <el-table-column label="状态" width="100" align="center">
        <template #default="{ row }">
          <el-tag :type="row.is_streaming ? 'warning' : 'info'" size="small">{{ row.is_streaming ? '进行中' : '空闲' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="160" align="center">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="view(row)">查看</el-button>
          <el-button size="small" text type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="viewVisible" :title="`会话 ${viewId}`" width="760px" top="6vh">
      <div style="max-height: 60vh; overflow-y: auto">
        <div v-for="(m, i) in viewMessages" :key="i" class="msg" :class="m.role === 'user' ? 'user' : 'assistant'">
          <div class="bubble" :style="m.role !== 'user' ? 'white-space:pre-wrap' : ''">{{ m.content }}</div>
        </div>
        <el-empty v-if="viewMessages.length === 0" description="无消息" />
      </div>
    </el-dialog>
  </div>
</template>
