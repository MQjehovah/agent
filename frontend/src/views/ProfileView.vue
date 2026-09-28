<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { api } from '../api'
import { useTheme } from '../theme'

use([CanvasRenderer, BarChart, PieChart, GridComponent, LegendComponent, TooltipComponent])

/** 个人中心(对齐桌面端): 账号信息 + 算力网关用量(今日/本月/余额/限流/配额/模型分布)。 */
interface UsageBucket { tokensIn: number; tokensOut: number; tokens: number; cost: number }
interface UsageModelRow {
  name: string
  today: UsageBucket
  month: UsageBucket
  dailyQuota: number
  monthlyQuota: number
}
interface UsageSummary {
  balance: number
  rateLimit: number
  quota: { daily: number; monthly: number }
  today: UsageBucket
  month: UsageBucket
  models: UsageModelRow[]
  truncated: boolean
  fetchedAt: string
}

const { theme } = useTheme()

const loading = ref(false)
const error = ref('')
const summary = ref<UsageSummary | null>(null)
const me = ref<{ name: string; display_name?: string; role: string }>({ name: '', display_name: '', role: '' })
const department = ref('')

const roleLabel = computed(() => {
  const r = me.value.role
  if (r === 'admin') return '管理员'
  if (r === 'user') return '普通用户'
  return '成员'
})

const models = computed(() => summary.value?.models ?? [])
const monthTotal = computed(() => models.value.reduce((sum, m) => sum + (m.month.tokens || 0), 0))

/** 主题相关图表配色(与桌面端一致) */
const chartTheme = computed(() => {
  const light = theme.value === 'light'
  return {
    text: light ? '#475569' : '#94a3b8',
    axisLine: { lineStyle: { color: light ? 'rgba(100,116,139,.28)' : 'rgba(148,163,184,.16)' } },
    axisLabel: { color: light ? '#334155' : '#64748b', fontSize: 11, interval: 0 },
    splitLine: { lineStyle: { color: light ? 'rgba(100,116,139,.12)' : 'rgba(148,163,184,.08)' } },
    tooltipBg: light ? '#ffffff' : '#141d30',
    tooltipBorder: light ? 'rgba(100,116,139,.24)' : 'rgba(148,163,184,.2)',
    tooltipText: light ? '#1e293b' : '#e6edf7'
  }
})

const barOption = computed(() => {
  const t = chartTheme.value
  const names = models.value.map((m) => m.name)
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: t.tooltipBg,
      borderColor: t.tooltipBorder,
      textStyle: { color: t.tooltipText, fontSize: 12 }
    },
    legend: { data: ['今日 tokens', '本月 tokens'], textStyle: { color: t.text, fontSize: 11 }, top: 0, right: 0 },
    grid: { left: 8, right: 8, top: 34, bottom: 0, containLabel: true },
    xAxis: {
      type: 'category',
      data: names,
      axisLine: t.axisLine,
      axisLabel: { ...t.axisLabel, rotate: names.length > 4 ? 30 : 0 },
      splitLine: t.splitLine
    },
    yAxis: { type: 'value', axisLine: t.axisLine, axisLabel: t.axisLabel, splitLine: t.splitLine },
    series: [
      { name: '今日 tokens', type: 'bar', data: models.value.map((m) => m.today.tokens), itemStyle: { color: '#22d3ee' }, barMaxWidth: 26 },
      { name: '本月 tokens', type: 'bar', data: models.value.map((m) => m.month.tokens), itemStyle: { color: '#6366f1' }, barMaxWidth: 26 }
    ]
  }
})

const pieOption = computed(() => {
  const t = chartTheme.value
  const data = models.value.filter((m) => m.month.tokens > 0).map((m) => ({ name: m.name, value: m.month.tokens }))
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      backgroundColor: t.tooltipBg,
      borderColor: t.tooltipBorder,
      textStyle: { color: t.tooltipText, fontSize: 12 },
      formatter: '{b}: {c} ({d}%)'
    },
    legend: { bottom: 0, textStyle: { color: t.text, fontSize: 11 } },
    color: ['#22d3ee', '#6366f1', '#34d399', '#fbbf24', '#fb7185', '#38bdf8', '#a78bfa'],
    series: [
      {
        type: 'pie',
        radius: ['48%', '72%'],
        center: ['50%', '45%'],
        avoidLabelOverlap: true,
        itemStyle: { borderRadius: 6, borderColor: t.tooltipBg, borderWidth: 2 },
        label: { color: t.text, fontSize: 11, formatter: '{b}\n{c}' },
        labelLine: { lineStyle: { color: t.axisLine.lineStyle.color } },
        data
      }
    ]
  }
})

function fmtNum(v: number | undefined): string {
  return (Number(v) || 0).toLocaleString('zh-CN')
}
function money(v: number | undefined): string {
  return `¥${(Number(v) || 0).toFixed(2)}`
}
function quotaPct(used: number, quota: number): number {
  if (!quota) return 0
  return Math.min(100, Math.round((Number(used) || 0) / quota * 100))
}
function formatTime(t: string): string {
  const d = new Date(t)
  if (Number.isNaN(d.getTime())) return String(t)
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

async function load() {
  loading.value = true
  error.value = ''
  const jobs = [
    api<{ name: string; display_name?: string; role: string; department?: string }>('/api/auth/me')
      .then(d => {
        me.value = { name: d.name || '', display_name: d.display_name, role: d.role || '' }
        department.value = d.department ?? ''
      }).catch(() => {}),
    api<UsageSummary>('/api/me/gateway-usage')
      .then(d => { summary.value = d })
      .catch((e) => { error.value = (e as Error).message || '用量数据加载失败' })
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
        <h2>个人中心</h2>
        <div class="sub">
          {{ (me.display_name || me.name) ? `你好，${me.display_name || me.name}（${roleLabel}），这是你的个人中心` : '你的个人中心' }}
          <span v-if="summary" class="usage-updated">· 用量更新于 {{ formatTime(summary.fetchedAt) }}</span>
        </div>
      </div>
      <div class="actions"><el-button :icon="Refresh" @click="load">刷新</el-button></div>
    </div>

    <div class="section-title">账号信息</div>
    <div class="card profile-card">
      <div class="profile-row"><span class="profile-k">姓名</span><span>{{ me.display_name || me.name || '—' }}</span></div>
      <div class="profile-row"><span class="profile-k">工号</span><span class="mono">{{ me.name || '—' }}</span></div>
      <div class="profile-row"><span class="profile-k">角色</span><span>{{ roleLabel }}</span></div>
      <div class="profile-row"><span class="profile-k">部门</span><span>{{ department || '—' }}</span></div>
    </div>

    <div class="section-title">用量信息</div>

    <div v-if="error" class="card usage-state">
      <el-empty :description="error">
        <el-button size="small" @click="load">重试</el-button>
      </el-empty>
    </div>

    <template v-else-if="summary">
      <div class="card usage-note">仅统计算力网关（本人密钥）用量；agent / 知识库的用量不计入</div>

      <div class="stat-grid">
        <div class="stat-card no-link">
          <div class="label">今日 tokens</div>
          <div class="value">{{ fmtNum(summary.today.tokens) }}</div>
          <div class="hint">入 {{ fmtNum(summary.today.tokensIn) }} · 出 {{ fmtNum(summary.today.tokensOut) }}</div>
          <div class="cost">花费 {{ money(summary.today.cost) }}</div>
        </div>
        <div class="stat-card no-link">
          <div class="label">本月 tokens</div>
          <div class="value">{{ fmtNum(summary.month.tokens) }}</div>
          <div class="hint">入 {{ fmtNum(summary.month.tokensIn) }} · 出 {{ fmtNum(summary.month.tokensOut) }}</div>
          <div class="cost">花费 {{ money(summary.month.cost) }}</div>
        </div>
        <div class="stat-card no-link">
          <div class="label">余额</div>
          <div class="value">{{ money(summary.balance) }}</div>
          <div class="hint">算力网关账户余额</div>
        </div>
        <div class="stat-card no-link">
          <div class="label">限流</div>
          <div class="value">{{ fmtNum(summary.rateLimit) }}<span class="unit">次/分</span></div>
          <div class="hint">请求速率上限</div>
        </div>
      </div>

      <div class="card quota-card">
        <div class="chart-title">配额使用</div>
        <div class="quota-row">
          <div class="quota-head">
            <span>今日 tokens</span>
            <span class="quota-num">{{ fmtNum(summary.today.tokens) }} / {{ summary.quota.daily ? fmtNum(summary.quota.daily) : '不限' }}</span>
          </div>
          <el-progress v-if="summary.quota.daily" :percentage="quotaPct(summary.today.tokens, summary.quota.daily)" :stroke-width="10" :show-text="false" />
          <div v-else class="quota-unlimited">不限</div>
        </div>
        <div class="quota-row">
          <div class="quota-head">
            <span>本月 tokens</span>
            <span class="quota-num">{{ fmtNum(summary.month.tokens) }} / {{ summary.quota.monthly ? fmtNum(summary.quota.monthly) : '不限' }}</span>
          </div>
          <el-progress v-if="summary.quota.monthly" :percentage="quotaPct(summary.month.tokens, summary.quota.monthly)" :stroke-width="10" :show-text="false" />
          <div v-else class="quota-unlimited">不限</div>
        </div>
      </div>

      <div v-if="!models.length" class="card usage-state">
        <el-empty description="暂无模型用量数据" />
      </div>
      <template v-else>
        <div class="charts-grid">
          <div class="card chart-card">
            <div class="chart-title">模型 tokens（今日 / 本月）</div>
            <VChart class="chart" :option="barOption" autoresize />
          </div>
          <div class="card chart-card">
            <div class="chart-title">本月各模型 tokens 占比</div>
            <VChart v-if="monthTotal > 0" class="chart" :option="pieOption" autoresize />
            <el-empty v-else description="本月暂无模型用量" />
          </div>
        </div>
        <div v-if="summary.truncated" class="usage-truncated">仅展示前 {{ models.length }} 个模型</div>
      </template>
    </template>

    <el-skeleton v-else :rows="6" animated />
  </div>
</template>

<style scoped>
.section-title { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.usage-updated { font-size: 12.5px; color: var(--text-3); }
.usage-state { padding: 20px; }
.usage-note { font-size: 12.5px; color: var(--text-2); padding: 10px 14px; }
.stat-card.no-link { cursor: default; }
.stat-card .cost { margin-top: 6px; font-size: 12.5px; font-weight: 600; color: #10b981; }
.stat-card .unit { margin-left: 4px; font-size: 12px; font-weight: 400; color: var(--text-3); }
.profile-card { display: flex; flex-wrap: wrap; gap: 8px 40px; padding: 14px 18px; }
.profile-row { display: flex; align-items: center; gap: 10px; font-size: 13.5px; color: var(--text); }
.profile-k { color: var(--text-3); font-size: 12.5px; min-width: 32px; }
.quota-card { padding: 14px 18px; }
.chart-title { font-size: 13.5px; font-weight: 600; color: var(--text); margin-bottom: 10px; }
.quota-row + .quota-row { margin-top: 14px; }
.quota-head { display: flex; align-items: center; justify-content: space-between; font-size: 13px; color: var(--text-2); margin-bottom: 6px; }
.quota-num { color: var(--text-3); font-size: 12.5px; }
.quota-unlimited { font-size: 12.5px; color: var(--text-3); }
.charts-grid { display: grid; grid-template-columns: 7fr 5fr; gap: 14px; }
@media (max-width: 900px) { .charts-grid { grid-template-columns: 1fr; } }
.chart-card { padding: 14px 16px 8px; }
.chart { width: 100%; height: 300px; }
.usage-truncated { font-size: 12px; color: var(--text-3); text-align: right; }
</style>
