<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, del, post } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

interface User { id: number | string; name: string; display_name?: string; role: string; status?: string; department?: string }
interface Role { name: string; description?: string; allowed_tools?: string[] }

const users = ref<User[]>([])
const roles = ref<Role[]>([])
const loading = ref(false)
const dialog = ref(false)
const form = ref({ name: '', display_name: '', password: '', role: 'default' })

async function load() {
  loading.value = true
  try {
    const [u, r] = await Promise.all([
      api<{ users?: User[] } | User[]>('/api/rbac/users'),
      api<{ roles?: Role[] } | Role[]>('/api/rbac/roles')
    ])
    users.value = (Array.isArray(u) ? u : (u.users ?? [])) as User[]
    roles.value = (Array.isArray(r) ? r : (r.roles ?? [])) as Role[]
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

async function addUser() {
  if (!form.value.name || form.value.password.length < 4) {
    ElMessage.warning('用户名与至少 4 位密码必填')
    return
  }
  try {
    await post('/api/rbac/users', form.value)
    dialog.value = false
    form.value = { name: '', display_name: '', password: '', role: 'default' }
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function toggle(u: User) {
  try {
    await post(`/api/rbac/users/${u.id}/toggle`, {})
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function removeUser(u: User) {
  try {
    await ElMessageBox.confirm(`删除用户「${u.display_name || u.name}」?`, '确认', { type: 'warning' })
  } catch { return }
  try {
    await del(`/api/rbac/users/${u.id}`)
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div><h2>用户管理</h2><div class="sub">基于角色的访问控制(admin 可见)</div></div>
      <el-button type="primary" @click="dialog = true">新建用户</el-button>
    </div>

    <el-table :data="users" v-loading="loading">
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="name" label="用户名" min-width="140" />
      <el-table-column label="显示名" min-width="120">
        <template #default="{ row }">{{ row.display_name || '—' }}</template>
      </el-table-column>
      <el-table-column prop="role" label="角色" width="120">
        <template #default="{ row }"><el-tag size="small">{{ row.role }}</el-tag></template>
      </el-table-column>
      <el-table-column prop="department" label="部门" width="120" />
      <el-table-column label="状态" width="90" align="center">
        <template #default="{ row }">
          <el-tag :type="(row.status ?? 'active') === 'active' ? 'success' : 'danger'" size="small">
            {{ (row.status ?? 'active') === 'active' ? '正常' : '已禁用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="150" align="center">
        <template #default="{ row }">
          <el-button size="small" text :type="row.status === 'active' ? 'warning' : 'success'" @click="toggle(row)">
            {{ row.status === 'active' ? '禁用' : '启用' }}
          </el-button>
          <el-button size="small" text type="danger" @click="removeUser(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <h3 style="font-size: 14px; margin: 18px 0 8px">角色(共 {{ roles.length }})</h3>
    <el-table :data="roles" size="small">
      <el-table-column prop="name" label="角色" width="140" />
      <el-table-column label="可用工具">
        <template #default="{ row }">
          <code style="font-size: 11.5px">{{ (row.allowed_tools ?? []).join(', ') || '(无)' }}</code>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialog" title="新建用户" width="420px">
      <el-form label-width="80px">
        <el-form-item label="用户名"><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="显示名"><el-input v-model="form.display_name" placeholder="中文显示名(可选)" /></el-form-item>
        <el-form-item label="密码"><el-input v-model="form.password" type="password" show-password /></el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role">
            <el-option v-for="r in roles" :key="r.name" :label="r.name" :value="r.name" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" @click="addUser">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>
