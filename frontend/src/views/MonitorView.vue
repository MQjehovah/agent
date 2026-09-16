<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { hasPerm } from '../api'
import MonitorOverview from './ops/MonitorOverview.vue'
import SessionsPanel from './ops/SessionsPanel.vue'
import SchedulerPanel from './ops/SchedulerPanel.vue'
import MemoriesPanel from './ops/MemoriesPanel.vue'
import WebhookPanel from './ops/WebhookPanel.vue'
import LogsPanel from './ops/LogsPanel.vue'

const route = useRoute()
const router = useRouter()

interface TabDef { key: string; label: string; perm: string }
const ALL_TABS: TabDef[] = [
  { key: 'overview', label: '总览', perm: 'admin.monitor' },
  { key: 'sessions', label: '会话管理', perm: 'admin.monitor' },
  { key: 'scheduler', label: '定时任务', perm: 'admin.scheduler' },
  { key: 'memories', label: '记忆管理', perm: 'admin.memories' },
  { key: 'webhook', label: 'Webhook', perm: 'admin.monitor' },
  { key: 'logs', label: '日志', perm: 'admin.logs' }
]
const tabs = computed(() => ALL_TABS.filter((t) => hasPerm(t.perm)))

function normalize(v: unknown): string {
  const keys = tabs.value.map((t) => t.key)
  return typeof v === 'string' && keys.includes(v) ? v : (keys[0] ?? 'overview')
}

const activeTab = ref<string>(normalize(route.query.tab))

watch(
  () => route.query.tab,
  (v) => {
    const n = normalize(v)
    if (n !== activeTab.value) activeTab.value = n
  }
)

watch(activeTab, (v) => {
  const cur = route.query.tab
  if (String(cur ?? '') !== v) {
    router.replace({ query: { ...route.query, tab: v } }).catch(() => {})
  }
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div><h2>运行监控</h2><div class="sub">运维综合页 · 全站/系统视角（按权限展示）</div></div>
    </div>

    <el-tabs v-model="activeTab" class="ops-tabs">
      <el-tab-pane v-for="t in tabs" :key="t.key" :label="t.label" :name="t.key">
        <MonitorOverview v-if="t.key === 'overview' && activeTab === 'overview'" />
        <SessionsPanel v-else-if="t.key === 'sessions' && activeTab === 'sessions'" />
        <SchedulerPanel v-else-if="t.key === 'scheduler' && activeTab === 'scheduler'" :scope-all="true" />
        <MemoriesPanel v-else-if="t.key === 'memories' && activeTab === 'memories'" :view-all="true" />
        <WebhookPanel v-else-if="t.key === 'webhook' && activeTab === 'webhook'" />
        <LogsPanel v-else-if="t.key === 'logs' && activeTab === 'logs'" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.ops-tabs :deep(.el-tabs__header) {
  margin: 0 0 16px;
}
.ops-tabs :deep(.el-tabs__nav-wrap::after) {
  height: 1px;
}
</style>
