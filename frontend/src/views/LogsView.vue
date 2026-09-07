<script setup lang="ts">
import LogsPanel from './ops/LogsPanel.vue'
</script>

<template>
  <div class="page" style="display: flex; flex-direction: column">
    <div class="page-head">
      <div><h2>运行日志</h2><div class="sub">实时推送(admin 直达)</div></div>
    </div>
    <LogsPanel />
  </div>
</template>
