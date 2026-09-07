<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import MonitorOverview from './ops/MonitorOverview.vue'
import SessionsPanel from './ops/SessionsPanel.vue'
import SchedulerPanel from './ops/SchedulerPanel.vue'
import MemoriesPanel from './ops/MemoriesPanel.vue'
import WebhookPanel from './ops/WebhookPanel.vue'
import LogsPanel from './ops/LogsPanel.vue'

const TAB_KEYS = ['overview', 'sessions', 'scheduler', 'memories', 'webhook', 'logs'] as const
type TabKey = typeof TAB_KEYS[number]

const route = useRoute()
const router = useRouter()

function normalize(v: unknown): TabKey {
  return (typeof v === 'string' && (TAB_KEYS as readonly string[]).includes(v) ? v : 'overview') as TabKey
}

const activeTab = ref<TabKey>(normalize(route.query.tab))

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
      <div><h2>运行监控</h2><div class="sub">运维综合页 · 全站/系统视角（管理员）</div></div>
    </div>

    <el-tabs v-model="activeTab" class="ops-tabs">
      <el-tab-pane label="总览" name="overview">
        <MonitorOverview v-if="activeTab === 'overview'" />
      </el-tab-pane>
      <el-tab-pane label="会话管理" name="sessions">
        <SessionsPanel v-if="activeTab === 'sessions'" />
      </el-tab-pane>
      <el-tab-pane label="定时任务" name="scheduler">
        <SchedulerPanel v-if="activeTab === 'scheduler'" :scope-all="true" />
      </el-tab-pane>
      <el-tab-pane label="记忆管理" name="memories">
        <MemoriesPanel v-if="activeTab === 'memories'" :view-all="true" />
      </el-tab-pane>
      <el-tab-pane label="Webhook" name="webhook">
        <WebhookPanel v-if="activeTab === 'webhook'" />
      </el-tab-pane>
      <el-tab-pane label="日志" name="logs">
        <LogsPanel v-if="activeTab === 'logs'" />
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
