<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import {
  ChatDotRound, Coin, Timer
} from '@element-plus/icons-vue'

const router = useRouter()

interface MyOverview {
  user: { uid: string; name: string; role: string }
  sessions: { total: number; running: number }
  memory: { mine: number; global: number }
}

const loading = ref(true)

const me = ref<MyOverview['user']>({ uid: '', name: '', role: '' })
const mySessions = ref({ total: 0, running: 0 })
const memory = ref({ mine: 0, global: 0 })
const schedulerTotal = ref(0)
const schedulerEnabled = ref(0)

const usageDays = ref(7)
const usageEnabled = ref(true)
const usage = ref<any>(null)

const roleLabel = computed(() => {
  const r = me.value.role
  if (r === 'admin') return '管理员'
  if (r === 'user') return '普通用户'
  return '成员'
})

const cards = computed(() => [
  { key: 'sessions', label: '我的会话', icon: ChatDotRound, value: mySessions.value.total,
    hint: `${mySessions.value.running} 个运行中`, to: '/chat' },
  { key: 'scheduler', label: '定时任务', icon: Timer, value: schedulerTotal.value,
    hint: `启用 ${schedulerEnabled.value} 个`, to: '/scheduler' },
  { key: 'memories', label: '我的记忆', icon: Coin, value: memory.value.mine + memory.value.global,
    hint: `个人 ${memory.value.mine} · 公共 ${memory.value.global}`, to: '/memories' }
])

const quick = [
  { path: '/chat', title: '发起新对话', sub: '与零号员工对话', icon: ChatDotRound },
  { path: '/scheduler', title: '定时任务', sub: '自动化例行工作', icon: Timer },
  { path: '/memories', title: '我的记忆', sub: '长期偏好与经验', icon: Coin }
]

function fmtNum(n: number | undefined): string {
  return (n ?? 0).toLocaleString()
}
function fmtMs(ms: number): string {
  if (ms == null || isNaN(ms)) return '—'
  if (ms >= 1000) return (ms / 1000).toFixed(1) + 's'
  return Math.round(ms) + 'ms'
}

async function loadUsage() {
  try {
    const d = await api<any>(`/api/usage?days=${usageDays.value}`)
    if (d.enabled === false) {
      usageEnabled.value = false
      return
    }
    usageEnabled.value = true
    usage.value = d
  } catch {
    /* 用量不可达时保持原状 */
  }
}

async function load() {
  loading.value = true
  const jobs = [
    api<MyOverview>('/api/my/overview').then(d => {
      me.value = d.user ?? me.value
      mySessions.value = d.sessions ?? { total: 0, running: 0 }
      memory.value = d.memory ?? { mine: 0, global: 0 }
    }).catch(() => {}),
    api('/api/scheduler/tasks').then(d => {
      schedulerTotal.value = (d.tasks ?? []).length
      schedulerEnabled.value = (d.tasks ?? []).filter((t: any) => t.enabled).length
    }).catch(() => {}),
    loadUsage()
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
        <h2>工作台</h2>
        <div class="sub">{{ me.name ? `你好，${me.name}（${roleLabel}），这是你的个人工作台` : '你的个人工作台' }}</div>
      </div>
      <div class="actions"><el-button @click="load">刷新</el-button></div>
    </div>

    <div class="stat-grid">
      <div v-for="c in cards" :key="c.key" class="stat-card" :class="{ 'no-link': !c.to }" @click="c.to && router.push(c.to)">
        <div class="label"><el-icon :size="14"><component :is="c.icon" /></el-icon>{{ c.label }}</div>
        <div class="value">{{ c.value }}</div>
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

    <div class="section-title">
      <span>我的用量</span>
      <span class="section-actions">
        <el-radio-group v-model="usageDays" size="small" @change="loadUsage">
          <el-radio-button :value="1">今日</el-radio-button>
          <el-radio-button :value="7">近7天</el-radio-button>
          <el-radio-button :value="30">近30天</el-radio-button>
        </el-radio-group>
      </span>
    </div>

    <el-empty v-if="usageEnabled === false" description="当前账号无身份标识，无法统计用量" />

    <template v-if="usage">
      <div class="stat-grid">
        <div class="stat-card">
          <div class="label">LLM 调用</div>
          <div class="value">{{ fmtNum(usage.totals.calls) }}</div>
          <div class="hint">{{ usageDays === 1 ? '今日' : `近${usageDays}天` }}</div>
        </div>
        <div class="stat-card">
          <div class="label">输入 tokens</div>
          <div class="value">{{ fmtNum(usage.totals.prompt_tokens) }}</div>
          <div class="hint">输出 {{ fmtNum(usage.totals.completion_tokens) }}</div>
        </div>
        <div class="stat-card">
          <div class="label">合计 tokens</div>
          <div class="value">{{ fmtNum(usage.totals.total_tokens) }}</div>
          <div class="hint">缓存命中 {{ fmtNum(usage.totals.cache_hit_tokens) }}</div>
        </div>
        <div class="stat-card">
          <div class="label">成本</div>
          <div class="value">¥{{ (usage.totals.cost ?? 0).toFixed(4) }}</div>
          <div class="hint">平均 {{ fmtMs(usage.totals.avg_duration_ms) }}</div>
        </div>
      </div>

      <div class="section-title">按日趋势</div>
      <div class="card" style="padding: 6px 0">
        <el-table :data="usage.by_day ?? []" size="small" empty-text="暂无记录">
          <el-table-column prop="key" label="日期" width="160" class-name="mono" />
          <el-table-column prop="calls" label="调用数" align="right" width="100">
            <template #default="{ row }">{{ fmtNum(row.calls) }}</template>
          </el-table-column>
          <el-table-column label="输入/输出 tokens" min-width="200">
            <template #default="{ row }">{{ fmtNum(row.prompt_tokens) }} / {{ fmtNum(row.completion_tokens) }}</template>
          </el-table-column>
          <el-table-column label="成本(¥)" align="right" width="110">
            <template #default="{ row }">{{ (row.cost ?? 0).toFixed(4) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div class="section-title">按模型</div>
      <div class="card" style="padding: 6px 0">
        <el-table :data="usage.by_model ?? []" size="small" empty-text="暂无记录">
          <el-table-column prop="key" label="模型" min-width="160" class-name="mono" />
          <el-table-column prop="calls" label="调用数" align="right" width="100">
            <template #default="{ row }">{{ fmtNum(row.calls) }}</template>
          </el-table-column>
          <el-table-column label="tokens" align="right" width="130">
            <template #default="{ row }">{{ fmtNum(row.total_tokens) }}</template>
          </el-table-column>
          <el-table-column label="成本(¥)" align="right" width="110">
            <template #default="{ row }">{{ (row.cost ?? 0).toFixed(4) }}</template>
          </el-table-column>
          <el-table-column label="平均耗时" align="right" width="110">
            <template #default="{ row }">{{ fmtMs(row.avg_duration_ms) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div class="section-title">最近调用</div>
      <div class="card" style="padding: 6px 0">
        <el-table :data="usage.recent ?? []" size="small" max-height="320" empty-text="暂无记录">
          <el-table-column label="时间" width="170">
            <template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template>
          </el-table-column>
          <el-table-column prop="model" label="模型" min-width="120" class-name="mono" />
          <el-table-column prop="prompt_tokens" label="输入" align="right" width="90">
            <template #default="{ row }">{{ fmtNum(row.prompt_tokens) }}</template>
          </el-table-column>
          <el-table-column prop="completion_tokens" label="输出" align="right" width="90">
            <template #default="{ row }">{{ fmtNum(row.completion_tokens) }}</template>
          </el-table-column>
          <el-table-column label="成本(¥)" align="right" width="100">
            <template #default="{ row }">{{ (row.cost ?? 0).toFixed(4) }}</template>
          </el-table-column>
          <el-table-column label="耗时" align="right" width="110">
            <template #default="{ row }">{{ fmtMs(row.duration_ms) }}</template>
          </el-table-column>
          <el-table-column prop="session_id" label="会话" min-width="170" class-name="mono" show-overflow-tooltip />
        </el-table>
      </div>
    </template>
  </div>
</template>

<style scoped>
.section-title { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.stat-card.no-link { cursor: default; }
</style>
