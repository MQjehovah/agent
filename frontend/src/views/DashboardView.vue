<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import {
  ChatDotRound, Clock, Connection, DataBoard, Monitor, Timer, Coin
} from '@element-plus/icons-vue'

const router = useRouter()
const loading = ref(true)

const agentStatus = ref<Record<string, any> | null>(null)
const sessionCount = ref(0)
const streamingCount = ref(0)
const kanbanStats = ref<Record<string, number>>({})
const schedulerEnabled = ref(0)
const schedulerTotal = ref(0)
const memoryTotal = ref(0)
const webhookRecent = ref<Array<{ task_id: string; status: string; created_at: string }>>([])
const modelName = ref('—')

const cards = computed(() => [
  { key: 'agent', label: 'Agent 状态', icon: Monitor, value: agentStatus.value ? '运行中' : '离线',
    hint: agentStatus.value?.model ? `模型 ${agentStatus.value.model}` : '—', ok: !!agentStatus.value, to: '/dashboard' },
  { key: 'sessions', label: '会话', icon: Clock, value: sessionCount.value, hint: `${streamingCount.value} 个进行中`, to: '/sessions' },
  { key: 'kanban', label: '看板任务', icon: DataBoard, value: Object.values(kanbanStats.value).reduce((a, b) => a + b, 0),
    hint: `进行中 ${kanbanStats.value['in_progress'] ?? 0} · 待办 ${kanbanStats.value['todo'] ?? 0}`, to: '/kanban' },
  { key: 'scheduler', label: '定时任务', icon: Timer, value: schedulerTotal.value, hint: `启用 ${schedulerEnabled.value} 个`, to: '/scheduler' },
  { key: 'memories', label: '记忆', icon: Coin, value: memoryTotal.value, hint: '长期记忆条目', to: '/memories' },
  { key: 'webhook', label: 'Webhook 任务', icon: Connection, value: webhookRecent.value.length, hint: '最近提交', to: '/webhook' }
])

const quick = [
  { path: '/chat', title: '发起新对话', sub: '与零号员工对话', icon: ChatDotRound },
  { path: '/kanban', title: '任务看板', sub: '规划与跟踪任务', icon: DataBoard },
  { path: '/scheduler', title: '定时任务', sub: '自动化例行工作', icon: Timer },
  { path: '/webhook', title: 'Webhook 监控', sub: '外部触发任务状态', icon: Connection }
]

async function load() {
  loading.value = true
  const jobs = [
    api('/api/agent/status').then(d => { agentStatus.value = d; modelName.value = String(d.model ?? '—') }).catch(() => { agentStatus.value = null }),
    api('/api/sessions').then(d => {
      const s = d.sessions ?? []
      sessionCount.value = s.length
      streamingCount.value = s.filter((x: any) => x.is_streaming).length
    }).catch(() => {}),
    api('/api/kanban').then(d => {
      const by: Record<string, number> = {}
      for (const t of (d.tasks ?? [])) by[t.column] = (by[t.column] ?? 0) + 1
      kanbanStats.value = by
    }).catch(() => {}),
    api('/api/scheduler/tasks').then(d => {
      schedulerTotal.value = (d.tasks ?? []).length
      schedulerEnabled.value = (d.tasks ?? []).filter((t: any) => t.enabled).length
    }).catch(() => {}),
    api('/api/memories?limit=1').then(d => { memoryTotal.value = d.total ?? 0 }).catch(() => {}),
    api('/webhook/tasks?limit=8').then(d => { webhookRecent.value = d.tasks ?? [] }).catch(() => {})
  ]
  await Promise.allSettled(jobs)
  loading.value = false
}

onMounted(load)
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-head">
      <div>
        <h2>总览</h2>
        <div class="sub">零号员工运行状态与快捷入口</div>
      </div>
      <div class="actions"><el-button @click="load">刷新</el-button></div>
    </div>

    <div class="stat-grid">
      <div v-for="c in cards" :key="c.key" class="stat-card" @click="c.to && router.push(c.to)">
        <div class="label"><el-icon :size="14"><component :is="c.icon" /></el-icon>{{ c.label }}</div>
        <div class="value" :class="{ ok: c.key === 'agent' && c.ok, bad: c.key === 'agent' && !c.ok }">{{ c.value }}</div>
        <div class="hint">{{ c.hint }}</div>
      </div>
    </div>

    <div class="section-title">快捷入口</div>
    <div class="quick-grid">
      <a v-for="q in quick" :key="q.path" class="quick-item" :href="'#' + q.path">
        <span class="q-icon"><el-icon :size="16"><component :is="q.icon" /></el-icon></span>
        <span><div class="q-title">{{ q.title }}</div><div class="q-sub">{{ q.sub }}</div></span>
      </a>
    </div>

    <div class="section-title">最近 Webhook 任务</div>
    <div class="card" style="padding: 6px 0">
      <el-table :data="webhookRecent" size="small" empty-text="暂无任务(外部系统通过 /webhook/execute 提交)">
        <el-table-column prop="task_id" label="任务 ID" min-width="260" class-name="mono" />
        <el-table-column label="状态" width="120" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'completed' ? 'success' : row.status === 'failed' ? 'danger' : row.status === 'running' ? 'warning' : 'info'">
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="提交时间" width="180">
          <template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>
