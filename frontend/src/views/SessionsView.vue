<script setup lang="ts">
defineOptions({ name: 'SessionsView' })
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import MarkdownIt from 'markdown-it'
import { api } from '../api'
import { channelMeta } from '../channel'

/** 个人「会话历史」:本人跨渠道会话（与对话页侧栏同源），可查看/继续/删除。 */
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
const router = useRouter()

interface SessionRow {
  id: string
  channel: string
  created_at?: string
  last_accessed?: string
  message_count?: number
  is_streaming?: boolean
}
interface HistoryMsg { role: string; content: string }

const loading = ref(false)
const rows = ref<SessionRow[]>([])

async function refresh(): Promise<void> {
  loading.value = true
  try {
    const [hist, live] = await Promise.all([
      api<{ sessions: any[] }>('/api/agent/sessions/history?limit=200').catch(() => ({ sessions: [] })),
      api<{ sessions: any[] }>('/api/sessions').catch(() => ({ sessions: [] }))
    ])
    const map = new Map<string, SessionRow>()
    for (const h of hist.sessions ?? []) {
      if (!h.id) continue
      map.set(h.id, {
        id: h.id,
        channel: h.channel || 'other',
        created_at: h.first_accessed,
        last_accessed: h.last_accessed,
        message_count: h.messages ?? 0,
        is_streaming: false
      })
    }
    for (const l of live.sessions ?? []) {
      if (!l.id) continue
      const prev = map.get(l.id)
      map.set(l.id, {
        id: l.id,
        channel: prev?.channel || 'web',
        created_at: prev?.created_at || l.created_at,
        last_accessed: prev?.last_accessed || l.created_at,
        message_count: l.message_count ?? prev?.message_count ?? 0,
        is_streaming: !!l.is_streaming
      })
    }
    rows.value = Array.from(map.values()).sort((a, b) =>
      (b.last_accessed || b.created_at || '').localeCompare(a.last_accessed || a.created_at || ''))
  } finally {
    loading.value = false
  }
}

function fmtTime(t?: string | number): string {
  if (t === undefined || t === null || t === '') return '—'
  const d = new Date(t)
  return Number.isNaN(d.getTime()) ? String(t) : d.toLocaleString()
}

/** 仅 web 会话可在「对话」页继续;钉钉等外部渠道只读。 */
function canContinue(row: SessionRow): boolean {
  return row.channel === 'web' || row.id.startsWith('web:')
}

async function fetchMessages(row: SessionRow): Promise<HistoryMsg[]> {
  if (row.channel !== 'web' && !row.id.startsWith('web:')) {
    const d = await api<{ messages: HistoryMsg[] }>(`/api/agent/sessions/messages?session_id=${encodeURIComponent(row.id)}`)
    return d.messages ?? []
  }
  try {
    const d = await api<{ messages: HistoryMsg[] }>(`/api/sessions/${encodeURIComponent(row.id)}/messages`)
    return d.messages ?? []
  } catch {
    const d = await api<{ messages: HistoryMsg[] }>(`/api/agent/sessions/messages?session_id=${encodeURIComponent(row.id)}`)
    return d.messages ?? []
  }
}

const viewVisible = ref(false)
const viewId = ref('')
const viewMsgs = ref<HistoryMsg[]>([])
const viewLoading = ref(false)

async function openView(row: SessionRow): Promise<void> {
  viewId.value = row.id
  viewVisible.value = true
  viewLoading.value = true
  try {
    viewMsgs.value = await fetchMessages(row)
  } catch (e) {
    ElMessage.error(`加载消息失败:${(e as Error).message}`)
    viewMsgs.value = []
  } finally {
    viewLoading.value = false
  }
}

function continueSession(row: SessionRow): void {
  if (!canContinue(row)) {
    ElMessage.info('该会话来自外部渠道，仅支持查看历史，请在对应渠道继续对话')
    return
  }
  void router.push({ path: '/chat', query: { session: row.id } })
}

async function removeSession(row: SessionRow): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认删除会话 ${row.id}？删除后不再显示。`, '删除会话', { type: 'warning' })
  } catch {
    return
  }
  try {
    await api(`/api/sessions/${encodeURIComponent(row.id)}`, { method: 'DELETE' })
    ElMessage.success('已删除')
    await refresh()
  } catch (e) {
    ElMessage.error(`删除失败:${(e as Error).message}`)
  }
}

function renderMd(content: string): string {
  return md.render(content ?? '')
}

const channelLabel = (row: SessionRow): string => channelMeta(row.channel, row.id).label
const channelType = (row: SessionRow) => channelMeta(row.channel, row.id).type

onMounted(() => void refresh())
</script>

<template>
  <div class="sessions-view">
    <div class="view-header">
      <span class="view-title">会话历史</span>
      <el-button :icon="Refresh" size="small" :loading="loading" @click="refresh">刷新</el-button>
    </div>

    <el-table :data="rows" v-loading="loading" height="100%" empty-text="暂无会话" style="width: 100%">
      <el-table-column label="会话" min-width="280" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="mono">{{ row.id }}</span>
          <el-tag v-if="row.is_streaming" size="small" type="warning" class="session-tag">进行中</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="渠道" width="110" align="center">
        <template #default="{ row }">
          <el-tag size="small" effect="plain" :type="channelType(row)">{{ channelLabel(row) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="180">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="消息数" width="90" align="center">
        <template #default="{ row }">{{ row.message_count ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="220" align="center">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="openView(row)">查看</el-button>
          <el-tooltip :disabled="canContinue(row)" content="外部渠道仅支持查看历史，请在对应渠道继续对话" placement="top">
            <span>
              <el-button size="small" text type="primary" :disabled="!canContinue(row)" @click="continueSession(row)">继续对话</el-button>
            </span>
          </el-tooltip>
          <el-button size="small" text type="danger" @click="removeSession(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="viewVisible" :title="`会话 ${viewId}`" width="760px" top="6vh">
      <div v-loading="viewLoading" class="session-messages">
        <div v-for="(m, i) in viewMsgs" :key="i" class="msg-row" :class="m.role === 'user' ? 'user' : 'assistant'">
          <div v-if="m.role === 'user'" class="user-bubble">{{ m.content }}</div>
          <div v-else class="assistant-text md" v-html="renderMd(m.content)" />
        </div>
        <el-empty v-if="!viewLoading && viewMsgs.length === 0" description="无消息" />
      </div>
    </el-dialog>
  </div>
</template>
