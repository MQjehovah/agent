<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import SchedulerPanel from './ops/SchedulerPanel.vue'

const route = useRoute()
const scopeAll = computed(() => route.query.scope === 'all')
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div><h2>定时任务</h2>
        <div class="sub">{{ scopeAll ? '全部用户与系统级(static)定时任务（运维全量）' : '按 Cron 周期自动执行的任务（仅显示我创建的）' }}</div>
      </div>
    </div>
    <SchedulerPanel :scope-all="scopeAll" />
  </div>
</template>
