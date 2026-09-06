<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { ElMessage } from 'element-plus'

const days = ref(7)
const loading = ref(false)
const data = ref<any>(null)
const enabled = ref(true)

function fmtNum(n: number | undefined): string {
  return (n ?? 0).toLocaleString()
}
function fmtMs(ms: number): string {
  if (ms == null || isNaN(ms)) return '—'
  if (ms >= 1000) return (ms / 1000).toFixed(1) + 's'
  return Math.round(ms) + 'ms'
}

async function load() {
  loading.value = true
  try {
    const d = await api<any>(`/api/usage?days=${days.value}`)
    if (d.enabled === false) {
      enabled.value = false
      return
    }
    enabled.value = true
    data.value = d
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

function onDaysChange() {
  void load()
}

onMounted(load)
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-head">
      <div><h2>我的用量</h2><div class="sub">本账号的 token 消耗与成本</div></div>
      <div class="actions">
        <el-radio-group v-model="days" size="small" @change="onDaysChange">
          <el-radio-button :value="1">今日</el-radio-button>
          <el-radio-button :value="7">近7天</el-radio-button>
          <el-radio-button :value="30">近30天</el-radio-button>
        </el-radio-group>
        <el-button style="margin-left:12px" @click="load">刷新</el-button>
      </div>
    </div>

    <el-empty v-if="enabled === false" description="当前账号无身份标识，无法统计用量" />

    <template v-if="data">
      <div class="stat-grid">
        <div class="stat-card">
          <div class="label">LLM 调用</div>
          <div class="value">{{ fmtNum(data.totals.calls) }}</div>
          <div class="hint">{{ days === 1 ? '今日' : `近${days}天` }}</div>
        </div>
        <div class="stat-card">
          <div class="label">输入 tokens</div>
          <div class="value">{{ fmtNum(data.totals.prompt_tokens) }}</div>
          <div class="hint">输出 {{ fmtNum(data.totals.completion_tokens) }}</div>
        </div>
        <div class="stat-card">
          <div class="label">合计 tokens</div>
          <div class="value">{{ fmtNum(data.totals.total_tokens) }}</div>
          <div class="hint">缓存命中 {{ fmtNum(data.totals.cache_hit_tokens) }}</div>
        </div>
        <div class="stat-card">
          <div class="label">成本</div>
          <div class="value">¥{{ (data.totals.cost ?? 0).toFixed(4) }}</div>
          <div class="hint">平均 {{ fmtMs(data.totals.avg_duration_ms) }}</div>
        </div>
      </div>

      <div class="section-title">按日趋势</div>
      <div class="card" style="padding: 6px 0">
        <el-table :data="data.by_day ?? []" size="small" empty-text="暂无记录">
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
        <el-table :data="data.by_model ?? []" size="small" empty-text="暂无记录">
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
        <el-table :data="data.recent ?? []" size="small" max-height="320" empty-text="暂无记录">
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
