<script setup lang="ts">
import { ref } from 'vue'
import { useSettingsStore } from '../stores/settings'
import logoUrl from '../assets/logo.svg'

const settings = useSettingsStore()
const error = ref('')
const loading = ref(false)

/** 企业账号 SSO:系统浏览器完成认证,SSO token 由主进程持有 */
async function loginWithSso() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    await settings.loginSso()
  } catch (err) {
    error.value = (err as Error).message || 'SSO 登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <img class="login-logo" :src="logoUrl" alt="Rosiwit" />
      <h1>员工 AI 工作台</h1>
      <p class="login-sub">使用公司统一账号登录</p>

      <div v-if="error" class="login-error">{{ error }}</div>

      <button class="f-btn" type="button" :disabled="loading" @click="loginWithSso">
        {{ loading ? '正在打开浏览器…' : '企业账号 SSO 登录' }}
      </button>

      <p class="login-hint">认证完成后将自动返回,凭据仅在主进程内使用</p>
    </div>
  </div>
</template>
