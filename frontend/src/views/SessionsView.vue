<script setup lang="ts">
import SessionsPanel from './ops/SessionsPanel.vue'
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div><h2>会话管理</h2><div class="sub">全部用户的历史会话记录与运行中会话（admin 直达）</div></div>
    </div>
    <SessionsPanel />
  </div>
</template>
