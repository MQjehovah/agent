<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import type { UsageSummary } from '../api/types'
import { useSettingsStore } from '../stores/settings'

use([CanvasRenderer, BarChart, PieChart, GridComponent, LegendComponent, TooltipComponent])

const router = useRouter()
const settings = useSettingsStore()

const loading = ref(false)
const error = ref('')
const summary = ref<UsageSummary | null>(null)

const models = computed(() => summary.value?.models ?? [])
const monthTotal = computed(() => models.value.reduce((sum, m) => sum + (m.month.tokens || 0), 0))

/** 主题相关图表配色:浅色下用深字,避免深色主题的浅字在浅色背景不可见 */
const chartTheme = computed(() => {
  const light = settings.theme === 'light'
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
      {
        name: '今日 tokens',
        type: 'bar',
        data: models.value.map((m) => m.today.tokens),
        itemStyle: { color: '#22d3ee' },
        barMaxWidth: 26
      },
      {
        name: '本月 tokens',
        type: 'bar',
        data: models.value.map((m) => m.month.tokens),
        itemStyle: { color: '#6366f1' },
        barMaxWidth: 26
      }
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

function fmtNum(v: number): string {
  return (Number(v) || 0).toLocaleString('zh-CN')
}

function money(v: number): string {
  return `¥${(Number(v) || 0).toFixed(2)}`
}

function quotaPct(used: number, quota: number): number {
  if (!quota) return 0
  return Math.min(100, Math.round(((Number(used) || 0) / quota) * 100))
}

function formatTime(t: string): string {
  const d = new Date(t)
  if (Number.isNaN(d.getTime())) return t
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function readableError(e: unknown): string {
  const msg = (e as Error)?.message ?? String(e)
  // Electron 会把主进程异常包装成 "Error invoking remote method 'x': Error: 真实信息"
  return msg.replace(/^Error invoking remote method '[^']*':\s*(Error:\s*)?/, '')
}

async function load() {
  if (!settings.hasUser) return
  loading.value = true
  error.value = ''
  try {
    // 主进程 handler 直接返回 UsageSummary,失败以 reject 形式抛出,故在此 catch
    summary.value = await window.desktop.invoke<UsageSummary>('usage:get')
  } catch (err) {
    error.value = readableError(err) || '用量数据加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  if (!settings.hasUser) return
  void load()
})
</script>

<template>
  <div class="usage-view">
    <header class="view-header">
      <div class="view-header-left">
        <span class="view-title">个人中心</span>
        <span v-if="summary" class="usage-updated">用量更新于 {{ formatTime(summary.fetchedAt) }}</span>
      </div>
      <el-button :icon="Refresh" size="small" :loading="loading" :disabled="!settings.hasUser" @click="load">
        刷新
      </el-button>
    </header>

    <!-- 账号信息(原侧栏「个人中心」弹窗内容) -->
    <el-card class="profile-card" shadow="never">
      <div class="profile-grid">
        <div class="profile-item">
          <span class="profile-label">工号</span>
          <span class="profile-value mono">{{ settings.user?.id || '—' }}</span>
        </div>
        <div class="profile-item">
          <span class="profile-label">姓名</span>
          <span class="profile-value">{{ settings.user?.name || '—' }}</span>
        </div>
        <div class="profile-item">
          <span class="profile-label">部门</span>
          <span class="profile-value">{{ settings.user?.department || '—' }}</span>
        </div>
        <div class="profile-item">
          <span class="profile-label">角色</span>
          <span class="profile-value">{{ settings.user?.role || '—' }}</span>
        </div>
        <div class="profile-item">
          <span class="profile-label">认证方式</span>
          <span class="profile-value">LDAP 统一认证</span>
        </div>
      </div>
    </el-card>

    <div class="profile-section-title">用量</div>

    <div v-loading="loading" class="usage-body">
      <!-- 未登录:先完成企业 SSO 登录 -->
      <div v-if="!settings.hasUser" class="usage-state">
        <el-empty description="登录后可查看用量">
          <el-button type="primary" size="small" @click="router.push('/settings')">前往登录</el-button>
        </el-empty>
      </div>

      <!-- 取数失败 -->
      <div v-else-if="error" class="usage-state">
        <el-empty :description="error">
          <el-button size="small" @click="load">重试</el-button>
        </el-empty>
      </div>

      <!-- 首次加载:骨架 -->
      <el-skeleton v-else-if="!summary" :rows="8" animated class="usage-skeleton" />

      <div v-else class="usage-content">
        <el-alert
          class="usage-scope"
          type="info"
          :closable="false"
          show-icon
          title="仅统计桌面端（算力网关）用量；agent / 知识库的用量不计入"
        />

        <!-- 概览卡片 -->
        <el-row :gutter="12" class="usage-cards">
          <el-col :xs="24" :sm="12" :md="6">
            <el-card shadow="never" class="usage-card">
              <div class="usage-card-label">今日 tokens</div>
              <div class="usage-card-value">{{ fmtNum(summary.today.tokens) }}</div>
              <div class="usage-card-sub">入 {{ fmtNum(summary.today.tokensIn) }} · 出 {{ fmtNum(summary.today.tokensOut) }}</div>
              <div class="usage-card-cost">花费 {{ money(summary.today.cost) }}</div>
            </el-card>
          </el-col>
          <el-col :xs="24" :sm="12" :md="6">
            <el-card shadow="never" class="usage-card">
              <div class="usage-card-label">本月 tokens</div>
              <div class="usage-card-value">{{ fmtNum(summary.month.tokens) }}</div>
              <div class="usage-card-sub">入 {{ fmtNum(summary.month.tokensIn) }} · 出 {{ fmtNum(summary.month.tokensOut) }}</div>
              <div class="usage-card-cost">花费 {{ money(summary.month.cost) }}</div>
            </el-card>
          </el-col>
          <el-col :xs="24" :sm="12" :md="6">
            <el-card shadow="never" class="usage-card">
              <div class="usage-card-label">余额</div>
              <div class="usage-card-value">{{ money(summary.balance) }}</div>
              <div class="usage-card-sub">算力网关账户余额</div>
            </el-card>
          </el-col>
          <el-col :xs="24" :sm="12" :md="6">
            <el-card shadow="never" class="usage-card">
              <div class="usage-card-label">限流</div>
              <div class="usage-card-value">{{ fmtNum(summary.rateLimit) }}<span class="usage-card-unit">次/分</span></div>
              <div class="usage-card-sub">请求速率上限</div>
            </el-card>
          </el-col>
        </el-row>

        <!-- 配额进度 -->
        <el-card shadow="never" class="usage-panel">
          <div class="usage-section-title">配额使用</div>
          <div class="usage-quota">
            <div class="usage-quota-head">
              <span>今日 tokens</span>
              <span class="usage-quota-num">
                {{ fmtNum(summary.today.tokens) }} / {{ summary.quota.daily ? fmtNum(summary.quota.daily) : '不限' }}
              </span>
            </div>
            <el-progress
              v-if="summary.quota.daily"
              :percentage="quotaPct(summary.today.tokens, summary.quota.daily)"
              :stroke-width="10"
              :show-text="false"
            />
            <div v-else class="usage-unlimited">不限</div>
          </div>
          <div class="usage-quota">
            <div class="usage-quota-head">
              <span>本月 tokens</span>
              <span class="usage-quota-num">
                {{ fmtNum(summary.month.tokens) }} / {{ summary.quota.monthly ? fmtNum(summary.quota.monthly) : '不限' }}
              </span>
            </div>
            <el-progress
              v-if="summary.quota.monthly"
              :percentage="quotaPct(summary.month.tokens, summary.quota.monthly)"
              :stroke-width="10"
              :show-text="false"
            />
            <div v-else class="usage-unlimited">不限</div>
          </div>
        </el-card>

        <!-- 图表:无模型数据时整体空态,卡片与配额仍渲染 -->
        <div v-if="!models.length" class="usage-chart-empty">
          <el-empty description="暂无模型用量数据" />
        </div>
        <template v-else>
          <el-row :gutter="12" class="usage-charts">
            <el-col :xs="24" :md="14">
              <el-card shadow="never" class="usage-panel">
                <div class="usage-section-title">模型 tokens（今日 / 本月）</div>
                <VChart class="usage-chart" :option="barOption" autoresize />
              </el-card>
            </el-col>
            <el-col :xs="24" :md="10">
              <el-card shadow="never" class="usage-panel">
                <div class="usage-section-title">本月各模型 tokens 占比</div>
                <VChart v-if="monthTotal > 0" class="usage-chart" :option="pieOption" autoresize />
                <el-empty v-else description="本月暂无模型用量" />
              </el-card>
            </el-col>
          </el-row>
          <div v-if="summary.truncated" class="usage-truncated">仅展示前 {{ models.length }} 个模型</div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
  .usage-view {
    display: flex;
    flex-direction: column;
    height: 100%;
  }

  .profile-card {
    flex: none;
    margin-bottom: 12px;
    border-radius: 12px;
  }

  .profile-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 10px 18px;
  }

  .profile-item {
    display: flex;
    align-items: baseline;
    gap: 8px;
    font-size: 13px;
    min-width: 0;
  }

  .profile-label {
    color: var(--el-text-color-secondary);
    font-size: 12.5px;
    flex: none;
  }

  .profile-value {
    color: var(--el-text-color-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }

  .profile-section-title {
    flex: none;
    margin: 2px 0 10px;
    font-size: 14px;
    font-weight: 600;
    color: var(--el-text-color-primary);
  }


.usage-updated {
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}

.usage-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.usage-state {
  padding-top: 12vh;
}

.usage-skeleton {
  padding: 20px 18px;
}

.usage-content {
  padding: 16px 18px 28px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.usage-scope {
  flex: none;
}

.usage-cards {
  row-gap: 12px;
}

.usage-card {
  height: 100%;
}

.usage-card-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.usage-card-value {
  font-size: 26px;
  font-weight: 600;
  line-height: 1.25;
  margin-top: 4px;
  word-break: break-all;
}

.usage-card-unit {
  font-size: 13px;
  font-weight: 400;
  margin-left: 4px;
  color: var(--el-text-color-secondary);
}

.usage-card-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 4px;
}

.usage-card-cost {
  font-size: 12.5px;
  margin-top: 6px;
  color: #10b981;
  font-weight: 600;
}

.usage-panel {
  flex: none;
}

.usage-section-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 12px;
}

.usage-quota + .usage-quota {
  margin-top: 16px;
}

.usage-quota-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  margin-bottom: 6px;
}

.usage-quota-num {
  color: var(--el-text-color-secondary);
}

.usage-unlimited {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.usage-charts {
  row-gap: 12px;
}

.usage-chart {
  width: 100%;
  height: 300px;
}

.usage-chart-empty {
  padding: 24px 0;
}

.usage-truncated {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-align: right;
}
</style>
