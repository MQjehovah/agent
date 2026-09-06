<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { post, setToken, setRole } from '../api'

const router = useRouter()
const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

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
    router.push('/chat')
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    loading.value = false
  }
}
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
    </div>
  </div>
</template>
