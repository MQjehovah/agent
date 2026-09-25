<script setup lang="ts">
import { onMounted, watchEffect } from 'vue'
import { useSettingsStore } from './stores/settings'
import LoginGate from './views/LoginGate.vue'
import QuickPromptView from './views/QuickPromptView.vue'

const settings = useSettingsStore()

/** 快速提问小窗走独立的 #/quick 入口,不渲染登录门与工作台 */
const isQuickWindow = window.location.hash.replace(/^#/, '').split('?')[0].startsWith('/quick')

onMounted(async () => {
  await settings.load()
})

// 主题跟随配置,切换即时生效
watchEffect(() => {
  document.documentElement.classList.toggle('dark', settings.theme === 'dark')
})
</script>

<template>
  <!-- 快速提问小窗:无边框置顶输入框 -->
  <QuickPromptView v-if="isQuickWindow" />

  <!-- 启动加载 -->
  <div v-else-if="!settings.loaded" class="boot-screen">正在启动…</div>

  <!-- 登录门:未认证时只显示登录页,认证通过才进入工作台 -->
  <LoginGate v-else-if="!settings.user" />

  <router-view v-else />
</template>
