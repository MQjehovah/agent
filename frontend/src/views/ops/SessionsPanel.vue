<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, del, hasPerm } from '../../api'
import { channelMeta, dingtalkGroupDisplayName, isDingtalkGroupSession } from '../../channel'
import { ElMessage, ElMessageBox } from 'element-plus'
import MarkdownIt from 'markdown-it'

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

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
interface HistoryMsg {
  role: string
  content: string
  name?: string
  tool_calls?: unknown[]
  tool_call_id?: string
  reasoning_content?: string
  created_at?: string
  user_id?: string
  channel?: string
  agent_id?: string
}
interface RunningRow {
  id: string
  conversation_id?: string
  channel?: string
  user?: { uid: string; name: string; display_name?: string }
  tag?: string
  started_at?: string
  duration_s?: number
  stage?: string
  model?: string
  is_streaming?: boolean
  worker?: boolean
}

const RUN_POLL_MS = 4000
const admin = hasPerm('admin.monitor')

const sessions = ref<SessionRow[]>([])
const loading = ref(false)
const running = ref<RunningRow[]>([])
const clock = ref(Date.now())
const viewVisible = ref(false)
const viewId = ref('')
const viewMessages = ref<HistoryMsg[]>([])
const runningIds = computed(() => new Set(running.value.map(r => r.conversation_id || r.id)))
const isViewRunning = computed(() => viewVisible.value && runningIds.value.has(viewId.value))

async function load() {
  loading.value = true
  try {
    const [live, hist] = await Promise.all([
      api<{ sessions: SessionRow[] }>('/api/sessions?scope=all').catch(() => ({ sessions: [] as SessionRow[] })),
      api<{ sessions: SessionRow[] }>('/api/agent/sessions/history?limit=200&scope=all').catch(() => ({ sessions: [] as SessionRow[] }))
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

async function loadRunning() {
  try {
    const ep = admin ? '/api/admin/sessions/running' : '/api/agent/sessions/running'
    const d = await api<{ sessions: RunningRow[] }>(ep)
    running.value = d.sessions ?? []
  } catch { /* 轮询失败静默,不打断 */ }
}

function fmtDuration(sec: number): string {
  sec = Math.max(0, Math.floor(sec))
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  if (h > 0) return `${h}时${String(m).padStart(2, '0')}分`
  if (m > 0) return `${m}分${String(s).padStart(2, '0')}秒`
  return `${s}秒`
}

function elapsed(row: RunningRow): string {
  const st = row.started_at
  if (st) {
    const ms = clock.value - new Date(st).getTime()
    if (!Number.isNaN(ms) && ms > 0) return fmtDuration(ms / 1000)
  }
  return row.duration_s != null ? fmtDuration(row.duration_s) : '—'
}

async function view(row: SessionRow) {
  viewId.value = row.id
  viewVisible.value = true
  viewMessages.value = []
  const paths = admin
    ? [
        `/api/admin/sessions/${encodeURIComponent(row.id)}/messages`,
        `/api/sessions/${encodeURIComponent(row.id)}/messages`,
        `/api/agent/sessions/messages?session_id=${encodeURIComponent(row.id)}`
      ]
    : [
        `/api/sessions/${encodeURIComponent(row.id)}/messages`,
        `/api/agent/sessions/messages?session_id=${encodeURIComponent(row.id)}`
      ]
  for (const p of paths) {
    try {
      const d = await api<{ messages: HistoryMsg[] }>(p)
      const msgs = d.messages ?? []
      if (msgs.length) {
        viewMessages.value = msgs
        return
      }
    } catch {
      /* 该来源无权限/不存在 → 试下一个 */
    }
  }
}

/** 审计视图辅助 */
function roleLabel(r: string): string {
  return r === 'user' ? '用户' : r === 'assistant' ? '助手' : r === 'tool' ? '工具结果' : r === 'system' ? '系统' : r || '?'
}
function roleTagType(r: string): 'primary' | 'success' | 'warning' | 'info' {
  return r === 'user' ? 'primary' : r === 'assistant' ? 'success' : r === 'tool' ? 'warning' : 'info'
}
function fmtTime(t?: string): string {
  if (!t) return ''
  const d = new Date(t)
  return Number.isNaN(d.getTime()) ? String(t) : d.toLocaleString()
}
function renderMd(text: string): string {
  return md.render(text ?? '')
}
interface ToolCallView { name: string; arguments: string }
function toolCallsOf(m: HistoryMsg): ToolCallView[] {
  const tcs = m.tool_calls
  if (!Array.isArray(tcs)) return []
  return tcs
    .map((t) => {
      const o = (t ?? {}) as Record<string, unknown>
      const fn = (o.function ?? {}) as Record<string, unknown>
      const name = String(o.name || fn.name || '')
      let args: unknown = o.arguments ?? fn.arguments ?? ''
      if (typeof args !== 'string') {
        try {
          args = JSON.stringify(args, null, 2)
        } catch {
          args = String(args)
        }
      }
      return { name, arguments: String(args) }
    })
    .filter((x) => x.name || x.arguments)
}

async function remove(row: SessionRow) {
  try {
    await ElMessageBox.confirm(`删除会话 ${row.id}?`, '确认', { type: 'warning' })
  } catch { return }
  try {
    await del(`/api/sessions/${encodeURIComponent(row.id)}`)
    ElMessage.success('已删除')
    await load()
    await loadRunning()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function refresh() {
  await Promise.all([load(), loadRunning()])
}

let pollTimer: number | undefined
let clockTimer: number | undefined
onMounted(() => {
  void load()
  void loadRunning()
  pollTimer = window.setInterval(() => void loadRunning(), RUN_POLL_MS)
  clockTimer = window.setInterval(() => {
    if (running.value.length) clock.value = Date.now()
  }, 1000)
})
onBeforeUnmount(() => {
  if (pollTimer) window.clearInterval(pollTimer)
  if (clockTimer) window.clearInterval(clockTimer)
})
</script>

<template>
  <div>
    <div class="run-card card">
      <div class="run-head">
        <div class="run-title">
          <span class="run-dot" />
          运行中
          <el-tag type="warning" effect="light" size="small" round>{{ running.length }}</el-tag>
        </div>
        <div class="run-sub">全部用户的正在执行会话(占用 Agent worker)</div>
      </div>
      <el-table :data="running" size="small" empty-text="当前无运行中" :show-header="running.length > 0">
        <el-table-column label="会话 ID" min-width="210" show-overflow-tooltip class-name="mono">
          <template #default="{ row }">{{ row.conversation_id || row.id }}</template>
        </el-table-column>
        <el-table-column label="渠道" width="96" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.channel" :type="channelMeta(row.channel, row.conversation_id || row.id).type" size="small">
              {{ channelMeta(row.channel, row.conversation_id || row.id).label }}
            </el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column v-if="admin" label="用户" width="130" show-overflow-tooltip>
          <template #default="{ row }">{{ row.user?.display_name || row.user?.name || row.user?.uid || row.tag || '-' }}</template>
        </el-table-column>
        <el-table-column label="模型" width="170" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="mono" style="font-size: 12px">{{ row.model || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="阶段" min-width="130" show-overflow-tooltip>
          <template #default="{ row }">
            <el-tag v-if="row.stage" type="info" effect="plain" size="small">{{ row.stage }}</el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="开始时间" width="170">
          <template #default="{ row }">
            {{ row.started_at ? new Date(row.started_at).toLocaleString() : '-' }}
          </template>
        </el-table-column>
        <el-table-column label="已运行" width="100" align="right" class-name="mono">
          <template #default="{ row }">{{ elapsed(row) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="120" align="center">
          <template #default="{ row }">
            <el-tag type="danger" effect="light" size="small" class="run-badge">
              <span class="run-dot" /> 正在运行
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="hist-head">
      <h3 class="section-title">历史会话(共 {{ sessions.length }})</h3>
      <el-button size="small" @click="refresh">刷新</el-button>
    </div>
    <el-table :data="sessions" v-loading="loading" empty-text="暂无会话">
      <el-table-column label="会话" min-width="240">
        <template #default="{ row }">
          <div class="sess-cell">
            <div v-if="isDingtalkGroupSession(row.id)" class="sess-group-name" :title="row.id">
              <span class="sess-group-icon">#</span>{{ dingtalkGroupDisplayName(row.id) }}
            </div>
            <div class="mono sess-full-id" :title="row.id">{{ row.id }}</div>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="来源" width="90" align="center">
        <template #default="{ row }">
          <el-tag :type="row.source === 'live' ? 'warning' : 'info'" size="small">
            {{ row.source === 'live' ? '活跃' : '历史' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="渠道" width="96" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.channel" :type="channelMeta(row.channel, row.id).type" size="small">
            {{ channelMeta(row.channel, row.id).label }}
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

    <el-dialog v-model="viewVisible" :title="`会话 ${viewId}`" width="900px" top="4vh">
      <el-alert
        v-if="isViewRunning"
        type="warning"
        :closable="false"
        show-icon
        title="该会话正在运行中"
        description="当前内容为进行中的快照，可能不完整，请稍后刷新查看最终结果。"
        style="margin-bottom: 10px"
      />
      <div class="audit-body">
        <div v-for="(m, i) in viewMessages" :key="i" class="audit-msg" :class="'r-' + (m.role || 'x')">
          <div class="audit-head">
            <el-tag size="small" :type="roleTagType(m.role)" effect="plain">{{ roleLabel(m.role) }}</el-tag>
            <span v-if="m.name" class="mono audit-name">{{ m.name }}</span>
            <span v-if="m.agent_id" class="mono audit-name">{{ m.agent_id }}</span>
            <span class="audit-time">{{ fmtTime(m.created_at) }}</span>
          </div>
          <details v-if="m.reasoning_content" class="audit-reason">
            <summary>思考过程</summary>
            <pre>{{ m.reasoning_content }}</pre>
          </details>
          <div v-for="(tc, ti) in toolCallsOf(m)" :key="ti" class="audit-tool">
            <div class="audit-tool-name mono">工具调用 · {{ tc.name || '(未命名)' }}</div>
            <pre class="audit-tool-args">{{ tc.arguments }}</pre>
          </div>
          <div v-if="m.content" class="audit-content">
            <div v-if="m.role === 'assistant' || m.role === 'system'" class="md" v-html="renderMd(m.content)" />
            <pre v-else>{{ m.content }}</pre>
          </div>
          <div v-if="m.role === 'tool' && m.tool_call_id" class="audit-tool-id mono">→ {{ m.tool_call_id }}</div>
        </div>
        <el-empty v-if="viewMessages.length === 0" description="无消息" />
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.run-card { margin-bottom: 6px; padding: 12px 14px 14px; }
.run-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.run-title { display: flex; align-items: center; gap: 7px; font-size: 13.5px; font-weight: 650; }
.run-sub { font-size: 11.5px; color: var(--text-3); }
.run-dot {
  width: 7px; height: 7px; border-radius: 50%;
  display: inline-block;
  background: var(--warn);
  animation: runpulse 1s infinite;
}
.run-badge .run-dot { background: currentColor; margin-right: 3px; }
.hist-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 14px; }
.hist-head .section-title { margin: 0; }
.sess-cell { display: flex; flex-direction: column; line-height: 1.4; padding: 2px 0; min-width: 0; }
.sess-group-name {
  display: flex; align-items: center; gap: 3px;
  font-size: 12px; color: var(--text-2);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sess-group-icon {
  color: var(--accent, #409eff); font-weight: 700;
  margin-right: 1px;
}
.sess-full-id {
  font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sess-cell .sess-full-id { opacity: 0.85; }
@keyframes runpulse { 50% { opacity: 0.35; } }

/* 审计视图: 完整消息(含工具调用/结果/思考) */
.audit-body { max-height: 66vh; overflow-y: auto; padding-right: 4px; }
.audit-msg { border: 1px solid var(--border); border-radius: 10px; padding: 8px 12px; margin-bottom: 10px; background: var(--bg-card); }
.audit-msg.r-user { border-left: 3px solid var(--accent, #409eff); }
.audit-msg.r-assistant { border-left: 3px solid var(--ok, #67c23a); }
.audit-msg.r-tool { border-left: 3px solid var(--warn, #e6a23c); background: var(--bg-inset); }
.audit-msg.r-system { border-left: 3px solid var(--text-3); }
.audit-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.audit-name { font-size: 12px; color: var(--text-2); }
.audit-time { margin-left: auto; font-size: 11.5px; color: var(--text-3); }
.audit-reason summary { font-size: 12px; color: var(--text-2); cursor: pointer; }
.audit-reason pre { margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; color: var(--text-2); background: var(--bg-inset); border-radius: 8px; padding: 8px 10px; }
.audit-tool { margin: 6px 0; border-left: 3px solid var(--accent, #409eff); padding-left: 8px; }
.audit-tool-name { font-size: 12.5px; font-weight: 600; }
.audit-tool-args { margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; background: var(--bg-inset); border-radius: 8px; padding: 8px 10px; max-height: 240px; overflow: auto; }
.audit-tool-id { margin-top: 4px; font-size: 11px; color: var(--text-3); }
.audit-content { margin-top: 4px; }
.audit-content pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: 13px; }
.audit-content .md { font-size: 14px; line-height: 1.7; }
</style>
