<script setup lang="ts">
import { ref } from 'vue'
import { api, post } from '../api'
import { ElMessage } from 'element-plus'

const oldPassword = ref('')
const newPassword = ref('')
const saving = ref(false)

async function changePassword() {
  if (newPassword.value.length < 4) {
    ElMessage.warning('新密码至少 4 位')
    return
  }
  saving.value = true
  try {
    await post('/api/auth/change-password', { old_password: oldPassword.value, new_password: newPassword.value })
    oldPassword.value = ''
    newPassword.value = ''
    ElMessage.success('密码已修改')
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    saving.value = false
  }
}

async function checkStatus() {
  try {
    const s = await api('/api/agent/status')
    ElMessage.success('Agent 正常:' + JSON.stringify(s).slice(0, 120))
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}
</script>

<template>
  <div class="page" style="max-width: 640px">
    <div class="page-head"><div><h2>设置</h2><div class="sub">账号与系统</div></div></div>

    <section style="border: 1px solid var(--el-border-color-lighter); border-radius: 10px; padding: 14px 18px; margin-bottom: 14px">
      <h3 style="font-size: 14px; margin: 0 0 12px">修改密码</h3>
      <el-form label-width="90px">
        <el-form-item label="当前密码"><el-input v-model="oldPassword" type="password" show-password /></el-form-item>
        <el-form-item label="新密码"><el-input v-model="newPassword" type="password" show-password /></el-form-item>
      </el-form>
      <el-button type="primary" :loading="saving" @click="changePassword">保存新密码</el-button>
    </section>

    <section style="border: 1px solid var(--el-border-color-lighter); border-radius: 10px; padding: 14px 18px">
      <h3 style="font-size: 14px; margin: 0 0 12px">系统状态</h3>
      <el-button @click="checkStatus">检查 Agent 状态</el-button>
    </section>
  </div>
</template>
