<script setup lang="ts">
import WebhookPanel from './ops/WebhookPanel.vue'
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div><h2>Webhook 任务监控</h2><div class="sub">外部系统经 <code class="mono">POST /webhook/execute</code> 提交的任务（admin 直达）</div></div>
    </div>
    <WebhookPanel />
  </div>
</template>
