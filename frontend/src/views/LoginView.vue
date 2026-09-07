<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { post, setToken, setRole } from '../api'

const router = useRouter()
const route = useRoute()
const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)
const ssoLoading = ref(false)

async function submit() {
  if (loading.value || !username.value.trim() || !password.value) return
  loading.value = true
  error.value = ''
  try {
    const data = await post<{ token: string; user?: { role?: string } }>('/api/auth/login', {
      username: username.value.trim(),
      password: password.value
    })
    setToken(data.token)
    setRole(data.user?.role ?? '')
    router.push('/dashboard')
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    loading.value = false
  }
}

/** 企业 SSO 登录:跳 SSO 授权,回调后携带 sso_token 回到本页 */
async function loginWithSso() {
  if (ssoLoading.value) return
  ssoLoading.value = true
  error.value = ''
  window.location.href = '/api/auth/sso/start'
}

/** SSO 回调:读取 sso_token 写入会话并进入工作台 */
function handleSsoCallback() {
  const q = route.query.sso_token
  if (typeof q === 'string' && q) {
    setToken(q)
    // 角色:回调未携带时默认空,后续 /api/auth/me 可补;此处保持最小
    router.replace('/dashboard')
  }
}

onMounted(handleSsoCallback)
</script>

<template>
  <div class="login-wrap">
    <div class="login-card">
      <div class="login-logo">零</div>
      <h1>零号员工</h1>
      <p class="login-sub">登录以使用公司数字员工服务</p>
      <el-alert v-if="error" :title="error" type="error" :closable="false" style="margin-bottom: 14px" />
      <el-form @submit.prevent="submit">
        <el-form-item><el-input v-model="username" placeholder="工号 / 用户名" autocomplete="username" @keyup.enter="submit" /></el-form-item>
        <el-form-item><el-input v-model="password" type="password" placeholder="密码" show-password autocomplete="current-password" @keyup.enter="submit" /></el-form-item>
        <el-button type="primary" style="width: 100%" size="large" :loading="loading" @click="submit">登 录</el-button>
      </el-form>
      <el-divider style="margin: 16px 0 12px"><span style="font-size: 12px; color: var(--el-text-color-secondary)">或</span></el-divider>
      <el-button size="large" style="width: 100%" :loading="ssoLoading" @click="loginWithSso">企业 SSO 登录</el-button>
    </div>
  </div>
</template>
