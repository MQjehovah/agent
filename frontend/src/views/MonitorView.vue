<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, post } from '../api'
import { ElMessage } from 'element-plus'

const loading = ref(false)
const auto = ref(true)
const stats = ref<any>(null)
const onlineCount = ref(0)
const nowText = ref('')

const running = computed<any[]>(() => stats.value?.running_sessions ?? [])

const days = ref(7)
const group = ref('day')
const usageData = ref<any>(null)

const audit = ref<any[]>([])
const auditLoading = ref(false)
const detailVisible = ref(false)
const detailId = ref('')
const detailMsgs = ref<any[]>([])
const threadsVisible = ref(false)
const threads = ref<any[]>([])

let timer: number | undefined
let clock: number | undefined

function channelMeta(ch?: string): { label: string; type: 'primary' | 'success' | 'info' | 'warning' } {
  switch (ch) {
    case 'web': return { label: 'Web', type: 'primary' }
    case 'dingtalk': return { label: '钉钉', type: 'success' }
    case 'feishu': return { label: '飞书', type: 'warning' }
    default: return { label: ch || '—', type: 'info' }
  }
}

async function loadStats() {
  try {
    const d = await api<any>('/api/admin/stats')
    stats.value = d
    onlineCount.value = d.online?.count ?? 0
  } catch (e) {
    if (auto.value) return
    ElMessage.error((e as Error).message)
  }
}

async function loadUsage() {
  try {
    usageData.value = await api<any>(`/api/admin/usage?days=${days.value}&group=${group.value}`)
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function loadAudit() {
  auditLoading.value = true
  try {
    const d = await api<any>('/api/admin/sessions?scope=history&limit=40')
    audit.value = d.history ?? []
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    auditLoading.value = false
  }
}

function fmtMs(ms: number): string {
  if (ms == null || isNaN(ms)) return '—'
  if (ms >= 1000) return (ms / 1000).toFixed(1) + 's'
  return Math.round(ms) + 'ms'
}

function fmtNum(n: number | undefined): string {
  return (n ?? 0).toLocaleString()
}

function durationText(s: number): string {
  if (!s || s <= 0) return '—'
  if (s < 60) return s + 's'
  const m = Math.floor(s / 60)
  const sec = s % 60
  return m + 'm' + (sec > 0 ? sec + 's' : '')
}

async function openDetail(row: any) {
  detailId.value = row.id
  detailVisible.value = true
  detailMsgs.value = []
  try {
    const d = await api<any>(`/api/admin/sessions/${encodeURIComponent(row.id)}/messages`)
    detailMsgs.value = d.messages ?? []
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function openThreads(row: any) {
  detailId.value = row.id
  try {
    const d = await api<any>(`/api/admin/sessions/${encodeURIComponent(row.id)}/threads`)
    threads.value = d.threads ?? []
    threadsVisible.value = true
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

function viewThread(sessionId: string) {
  threadsVisible.value = false
  detailId.value = sessionId
  detailVisible.value = true
  detailMsgs.value = []
  void (async () => {
    try {
      const d = await api<any>(`/api/admin/sessions/${encodeURIComponent(sessionId)}/messages`)
      detailMsgs.value = d.messages ?? []
    } catch (e) {
      ElMessage.error((e as Error).message)
    }
  })()
}

async function exportSession(row: any) {
  try {
    const d = await post<any>(`/api/admin/sessions/${encodeURIComponent(row.id)}/messages`, {})
    const blob = new Blob([JSON.stringify(d, null, 2)], { type: 'application/json;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `session-${row.id.replace(/[^\w.-]/g, '_')}-${Date.now()}.json`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

function onDaysChange() {
  void loadUsage()
}

function onGroupChange() {
  void loadUsage()
}

function refreshAll() {
  void loadStats()
  void loadUsage()
  void loadAudit()
}

onMounted(() => {
  refreshAll()
  timer = window.setInterval(() => {
    if (auto.value) void loadStats()
  }, 3000)
  clock = window.setInterval(() => {
    nowText.value = new Date().toLocaleTimeString()
  }, 1000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
  if (clock) window.clearInterval(clock)
})
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-head">
      <div><h2>运行监控</h2><div class="sub">全局系统视角 · 运行中会话 · worker 池 · MCP · token 用量 · 审计（管理员）</div></div>
      <div class="actions">
        <el-switch v-model="auto" active-text="自动刷新" style="margin-right: 14px" />
        <el-button @click="refreshAll">刷新 {{ nowText }}</el-button>
      </div>
    </div>

    <!-- 实时状态卡片 -->
    <div class="stat-grid" v-if="stats">
      <div class="stat-card">
        <div class="label">Agent</div>
        <div class="value ok">{{ stats.agent.name }}</div>
        <div class="hint">模型 {{ stats.agent.model }} · {{ stats.agent.status }}</div>
      </div>
      <div class="stat-card">
        <div class="label">Worker 池</div>
        <div class="value" :class="stats.pool?.enabled ? 'ok' : 'bad'">{{ stats.pool?.enabled ? `${stats.pool.active}/${stats.pool.capacity}` : '未启用' }}</div>
        <div class="hint">忙碌 {{ stats.pool?.busy ?? 0 }} · 已建 {{ stats.pool?.total_created ?? 0 }}{{ stats.pool?.enabled && (stats.pool.users || []).length ? ` · 在线 ${(stats.pool.users || []).length} 用户` : '' }}</div>
      </div>
      <div class="stat-card" v-if="stats.mcp">
        <div class="label">MCP 服务</div>
        <div class="value">{{ stats.mcp.count }}</div>
        <div class="hint">{{ stats.mcp.tools }} 个工具已加载</div>
      </div>
      <div class="stat-card">
        <div class="label">正在运行的会话</div>
        <div class="value ok">{{ stats.concurrency.running_streams }}</div>
        <div class="hint">共 {{ stats.concurrency.web_sessions }} 个活跃会话</div>
      </div>
      <div class="stat-card">
        <div class="label">Agent 会话 / 子代理</div>
        <div class="value">{{ stats.concurrency.agent_active_sessions }}</div>
        <div class="hint">运行任务 {{ stats.concurrency.running_tasks }} · 子代理 {{ stats.concurrency.subagent_active }}</div>
      </div>
      <div class="stat-card">
        <div class="label">今日 LLM 调用</div>
        <div class="value">{{ fmtNum(stats.usage_today.calls) }}</div>
        <div class="hint">{{ fmtNum(stats.usage_today.total_tokens) }} tokens · ¥{{ (stats.usage_today.cost ?? 0).toFixed(4) }}</div>
      </div>
      <div class="stat-card">
        <div class="label">在线用户</div>
        <div class="value">{{ onlineCount }}</div>
        <div class="hint">最近活跃 / 运行中</div>
      </div>
      <div class="stat-card">
        <div class="label">性能(今日)</div>
        <div class="value">{{ fmtMs(stats.perf_today.p50_duration_ms) }}</div>
        <div class="hint">P50 · P95 {{ fmtMs(stats.perf_today.p95_duration_ms) }}</div>
      </div>
    </div>

    <!-- 全站运行中会话：占用 Agent worker / 流式中 / 渠道执行(非 web)，全站口径 -->
    <div class="section-title">
      <span>全站运行中会话</span>
      <el-tag type="warning" effect="light" size="small" round>{{ running.length }}</el-tag>
    </div>
    <div class="card" style="padding: 6px 0">
      <el-table :data="running" size="small" empty-text="当前没有正在执行的全站会话">
        <el-table-column label="会话 ID" min-width="210" show-overflow-tooltip class-name="mono">
          <template #default="{ row }">{{ row.conversation_id || row.id }}</template>
        </el-table-column>
        <el-table-column label="渠道" width="86" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.channel" :type="channelMeta(row.channel).type" size="small">
              {{ channelMeta(row.channel).label }}
            </el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="用户" width="130" show-overflow-tooltip>
          <template #default="{ row }">{{ row.user?.name || row.user?.uid || row.tag || '-' }}</template>
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
        <el-table-column label="执行来源" width="100" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.worker ? 'warning' : 'info'" effect="plain">
              {{ row.worker ? 'Worker 池' : '渠道' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="已运行" width="100" align="right" class-name="mono">
          <template #default="{ row }">{{ durationText(row.duration_s) }}</template>
        </el-table-column>
        <el-table-column label="开始时间" width="170">
          <template #default="{ row }">{{ row.started_at ? new Date(row.started_at).toLocaleString() : '-' }}</template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 实时会话 -->
    <div class="section-title">实时会话</div>
    <div class="card" style="padding: 6px 0">
      <el-table :data="stats?.live_sessions ?? []" size="small" empty-text="当前没有进行中的会话">
        <el-table-column prop="id" label="会话 ID" min-width="200" class-name="mono" show-overflow-tooltip />
        <el-table-column prop="owner" label="归属用户" width="110" show-overflow-tooltip>
          <template #default="{ row }">
            <span>{{ row.owner }} <code v-if="row.uid && row.uid !== row.owner" style="font-size:11px;opacity:.7">#{{ row.uid }}</code></span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="row.is_streaming ? 'warning' : 'info'" size="small">{{ row.is_streaming ? '运行中' : '空闲' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="已运行" width="110" align="center">
          <template #default="{ row }">{{ durationText(row.duration_s) }}</template>
        </el-table-column>
        <el-table-column prop="message_count" label="消息数" width="90" align="center" />
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 用量大盘 -->
    <div class="section-title">Token 用量 / 成本</div>
    <div class="card" style="padding: 14px">
      <div style="display:flex;gap:12px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
        <span style="font-size:12px;color:var(--el-text-color-secondary)">时间窗</span>
        <el-radio-group v-model="days" size="small" @change="onDaysChange">
          <el-radio-button :value="1">今日</el-radio-button>
          <el-radio-button :value="7">近7天</el-radio-button>
          <el-radio-button :value="30">近30天</el-radio-button>
        </el-radio-group>
        <span style="font-size:12px;color:var(--el-text-color-secondary);margin-left:10px">维度</span>
        <el-select v-model="group" size="small" style="width:150px" @change="onGroupChange">
          <el-option label="按天" value="day" />
          <el-option label="按用户" value="user" />
          <el-option label="按模型" value="model" />
          <el-option label="按会话" value="session" />
        </el-select>
        <div style="margin-left:auto;font-size:12px;color:var(--el-text-color-secondary)">
          <template v-if="usageData?.totals">
            共 {{ fmtNum(usageData.totals.calls) }} 次调用 · {{ fmtNum(usageData.totals.total_tokens) }} tokens
            · 缓存命中 {{ fmtNum(usageData.totals.cache_hit_tokens) }} · 成本 ¥{{ (usageData.totals.cost ?? 0).toFixed(4) }}
            · 平均 {{ fmtMs(usageData.totals.avg_duration_ms) }}
          </template>
        </div>
      </div>
      <el-table :data="usageData?.breakdown ?? []" size="small" max-height="320">
        <el-table-column prop="key" :label="group === 'day' ? '日期' : group === 'user' ? '用户' : group === 'model' ? '模型' : '会话'" min-width="160" class-name="mono" show-overflow-tooltip />
        <el-table-column prop="calls" label="调用数" width="90" align="right">
          <template #default="{ row }">{{ fmtNum(row.calls) }}</template>
        </el-table-column>
        <el-table-column label="输入 tokens" width="110" align="right">
          <template #default="{ row }">{{ fmtNum(row.prompt_tokens) }}</template>
        </el-table-column>
        <el-table-column label="输出 tokens" width="110" align="right">
          <template #default="{ row }">{{ fmtNum(row.completion_tokens) }}</template>
        </el-table-column>
        <el-table-column label="总计 tokens" width="110" align="right">
          <template #default="{ row }">{{ fmtNum(row.total_tokens) }}</template>
        </el-table-column>
        <el-table-column prop="cost" label="成本(¥)" width="100" align="right">
          <template #default="{ row }">{{ (row.cost ?? 0).toFixed(4) }}</template>
        </el-table-column>
        <el-table-column label="平均耗时" width="100" align="right">
          <template #default="{ row }">{{ fmtMs(row.avg_duration_ms) }}</template>
        </el-table-column>
        <el-table-column label="最大耗时" width="100" align="right">
          <template #default="{ row }">{{ fmtMs(row.max_duration_ms) }}</template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 会话审计 -->
    <div class="section-title">会话审计（全部落盘内容）</div>
    <div class="card" style="padding: 6px 0">
      <el-table :data="audit" v-loading="auditLoading" size="small" empty-text="暂无历史会话记录">
        <el-table-column prop="id" label="会话 ID" min-width="200" class-name="mono" show-overflow-tooltip />
        <el-table-column label="归属" width="120">
          <template #default="{ row }">{{ row.owner || row.uid || '(匿名/系统)' }}</template>
        </el-table-column>
        <el-table-column prop="agent_id" label="Agent" width="140" show-overflow-tooltip />
        <el-table-column prop="messages" label="主消息" width="90" align="center">
          <template #default="{ row }">
            {{ row.messages }}
            <span v-if="row.thread_count" style="font-size:11px;opacity:.65;color:var(--el-color-warning)">(+{{ row.thread_count }}线程)</span>
          </template>
        </el-table-column>
        <el-table-column label="最后活跃" width="170">
          <template #default="{ row }">{{ new Date(row.last_accessed).toLocaleString() }}</template>
        </el-table-column>
        <el-table-column label="操作" width="190" align="center">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openDetail(row)">查看</el-button>
            <el-button v-if="row.thread_count" size="small" text type="success" @click="openThreads(row)">线程</el-button>
            <el-button size="small" text type="warning" @click="exportSession(row)">导出</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="detailVisible" :title="`会话审计 ${detailId}`" width="860px" top="4vh">
      <div class="audit-scroll">
        <div v-for="(m, i) in detailMsgs" :key="i" class="audit-item">
          <div class="audit-meta">
            <el-tag size="small" :type="m.role === 'user' ? 'primary' : m.role === 'assistant' ? 'success' : 'info'">{{ m.role }}</el-tag>
            <code class="mono">{{ m.user_id || 'system' }}</code>
            <code v-if="m.channel" class="mono">{{ m.channel }}</code>
            <span class="mono" style="opacity:.7">{{ m.created_at ? new Date(m.created_at).toLocaleString() : '' }}</span>
          </div>
          <div class="audit-content">{{ m.content }}</div>
        </div>
        <el-empty v-if="detailMsgs.length === 0" description="无消息" />
      </div>
      <template #footer>
        <el-button type="primary" @click="exportSession({ id: detailId })">导出 JSON</el-button>
        <el-button @click="detailVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="threadsVisible" :title="`对话 ${detailId} 的子代理线程`" width="640px" top="8vh">
      <el-table :data="threads" size="small" empty-text="无内部线程">
        <el-table-column prop="session_id" label="线程 ID" min-width="220" class-name="mono" show-overflow-tooltip />
        <el-table-column prop="agent_id" label="Agent" width="160" show-overflow-tooltip />
        <el-table-column prop="msg_count" label="消息数" width="90" align="center" />
        <el-table-column label="操作" width="90" align="center">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="viewThread(row.session_id)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
      <template #footer><el-button @click="threadsVisible = false">关闭</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
.audit-scroll {
  max-height: 66vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.audit-item {
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  padding: 8px 10px;
  background: var(--el-fill-color-blank);
}
.audit-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  font-size: 12px;
  flex-wrap: wrap;
}
.audit-content {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  line-height: 1.6;
  max-height: 220px;
  overflow-y: auto;
}
</style>
