<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, del, hasPerm, post, put } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

interface User {
  id: number | string
  name: string
  display_name?: string
  role: string
  status?: string
  department?: string
  has_password?: boolean
}
interface Role {
  name: string
  description?: string
  allowed_tools?: string[]
  allowed_agents?: string[]
  permissions?: string[]
  data_scope?: string
  builtin?: boolean
}
interface Department {
  name: string
  description?: string
  manager_id?: number | null
  manager_name?: string
  member_count?: number
}
interface PermItem { key: string; label: string; description?: string }
interface PermCatalog { permissions: PermItem[]; data_scopes: PermItem[] }

const canUsers = hasPerm('admin.users')
const canRoles = hasPerm('admin.roles')
const canDepts = hasPerm('admin.departments')

const activeTab = ref(canUsers ? 'users' : canRoles ? 'roles' : 'departments')

const users = ref<User[]>([])
const roles = ref<Role[]>([])
const departments = ref<Department[]>([])
const catalog = ref<PermCatalog>({ permissions: [], data_scopes: [] })
const loading = ref(false)

// —— 用户 ——
const dialog = ref(false)
const form = ref({ name: '', display_name: '', password: '', role: 'default', department: '' })
const editDialog = ref(false)
const editTarget = ref<User | null>(null)
const editForm = ref({ role: '', display_name: '', department: '', password: '' })

// —— 角色 ——
const roleDialog = ref(false)
const roleEditing = ref(false)
const roleForm = ref({
  name: '', description: '', allowed_tools: '', allowed_agents: '',
  permissions: [] as string[], data_scope: 'self'
})

// —— 部门 ——
const deptDialog = ref(false)
const deptEditing = ref(false)
const deptForm = ref<{ name: string; description: string; manager_id: number | null }>({
  name: '', description: '', manager_id: null
})

const roleNames = computed(() => roles.value.map((r) => r.name))
const deptNames = computed(() => departments.value.map((d) => d.name))
const userOptions = computed(() => users.value.map((u) => ({ id: Number(u.id), name: u.display_name || u.name })))

async function load() {
  loading.value = true
  try {
    const jobs: Promise<void>[] = []
    if (canUsers) {
      jobs.push(api<{ users?: User[] } | User[]>('/api/rbac/users').then((u) => {
        users.value = (Array.isArray(u) ? u : (u.users ?? [])) as User[]
      }))
    }
    if (canRoles) {
      jobs.push(api<{ roles?: Role[] } | Role[]>('/api/rbac/roles').then((r) => {
        roles.value = (Array.isArray(r) ? r : (r.roles ?? [])) as Role[]
      }))
      jobs.push(api<PermCatalog>('/api/rbac/permissions').then((c) => { catalog.value = c }))
    } else if (canUsers) {
      // 用户表单需角色下拉；无角色管理权限时读取精简角色选项(后端按 admin.users 放行)
      jobs.push(api<{ roles?: Role[] }>('/api/rbac/roles/options').then((r) => {
        roles.value = (r.roles ?? []) as Role[]
      }).catch(() => { /* 忽略 */ }))
    }
    if (canDepts) {
      jobs.push(api<{ departments?: Department[] }>('/api/rbac/departments').then((d) => {
        departments.value = d.departments ?? []
      }))
    }
    await Promise.all(jobs)
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

// ================= 用户 =================
async function addUser() {
  if (!form.value.name || form.value.password.length < 4) {
    ElMessage.warning('用户名与至少 4 位密码必填')
    return
  }
  try {
    await post('/api/rbac/users', form.value)
    dialog.value = false
    form.value = { name: '', display_name: '', password: '', role: 'default', department: '' }
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

function openEdit(u: User) {
  editTarget.value = u
  editForm.value = {
    role: u.role,
    display_name: u.display_name ?? '',
    department: u.department ?? '',
    password: ''
  }
  editDialog.value = true
}

async function saveEdit() {
  const u = editTarget.value
  if (!u) return
  const body: Record<string, string> = { role: editForm.value.role }
  if (editForm.value.display_name !== (u.display_name ?? '')) body.display_name = editForm.value.display_name
  if (editForm.value.department !== (u.department ?? '')) body.department = editForm.value.department
  if (editForm.value.password) body.password = editForm.value.password
  try {
    await put(`/api/rbac/users/${u.id}`, body)
    editDialog.value = false
    ElMessage.success('已保存')
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

// ================= 角色 =================
function openRoleCreate() {
  roleEditing.value = false
  roleForm.value = { name: '', description: '', allowed_tools: '', allowed_agents: '', permissions: [], data_scope: 'self' }
  roleDialog.value = true
}

function openRoleEdit(r: Role) {
  roleEditing.value = true
  roleForm.value = {
    name: r.name,
    description: r.description ?? '',
    allowed_tools: (r.allowed_tools ?? []).join(', '),
    allowed_agents: (r.allowed_agents ?? []).join(', '),
    permissions: [...(r.permissions ?? [])],
    data_scope: r.data_scope ?? 'self'
  }
  roleDialog.value = true
}

function parseList(s: string): string[] {
  return s.split(',').map((x) => x.trim()).filter(Boolean)
}

async function saveRole() {
  const f = roleForm.value
  if (!roleEditing.value && !f.name.trim()) {
    ElMessage.warning('角色名必填')
    return
  }
  const body = {
    description: f.description,
    allowed_tools: parseList(f.allowed_tools),
    allowed_agents: parseList(f.allowed_agents),
    permissions: f.permissions,
    data_scope: f.data_scope
  }
  try {
    if (roleEditing.value) await put(`/api/rbac/roles/${encodeURIComponent(f.name)}`, body)
    else await post('/api/rbac/roles', { name: f.name.trim(), ...body })
    roleDialog.value = false
    ElMessage.success('已保存')
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function removeRole(r: Role) {
  try {
    await ElMessageBox.confirm(`删除角色「${r.name}」?`, '确认', { type: 'warning' })
  } catch { return }
  try {
    await del(`/api/rbac/roles/${encodeURIComponent(r.name)}`)
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

// ================= 部门 =================
function openDeptCreate() {
  deptEditing.value = false
  deptForm.value = { name: '', description: '', manager_id: null }
  deptDialog.value = true
}

function openDeptEdit(d: Department) {
  deptEditing.value = true
  deptForm.value = { name: d.name, description: d.description ?? '', manager_id: d.manager_id ?? null }
  deptDialog.value = true
}

async function saveDept() {
  const f = deptForm.value
  if (!f.name.trim()) {
    ElMessage.warning('部门名必填')
    return
  }
  try {
    if (deptEditing.value) {
      await put(`/api/rbac/departments/${encodeURIComponent(f.name)}`, {
        description: f.description, manager_id: f.manager_id
      })
    } else {
      await post('/api/rbac/departments', {
        name: f.name.trim(), description: f.description, manager_id: f.manager_id
      })
    }
    deptDialog.value = false
    ElMessage.success('已保存')
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function removeDept(d: Department) {
  try {
    await ElMessageBox.confirm(`删除部门「${d.name}」?`, '确认', { type: 'warning' })
  } catch { return }
  try {
    await del(`/api/rbac/departments/${encodeURIComponent(d.name)}`)
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
      <div><h2>用户与权限</h2><div class="sub">基于角色与部门的访问控制（按权限展示）</div></div>
      <div>
        <el-button v-if="canUsers && activeTab === 'users'" type="primary" @click="dialog = true">新建用户</el-button>
        <el-button v-if="canRoles && activeTab === 'roles'" type="primary" @click="openRoleCreate">新建角色</el-button>
        <el-button v-if="canDepts && activeTab === 'departments'" type="primary" @click="openDeptCreate">新建部门</el-button>
      </div>
    </div>

    <el-tabs v-model="activeTab">
      <el-tab-pane v-if="canUsers" label="用户" name="users">
        <el-table :data="users" v-loading="loading">
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column prop="name" label="工号/用户名" min-width="140" />
          <el-table-column label="显示名" min-width="120">
            <template #default="{ row }">{{ row.display_name || '—' }}</template>
          </el-table-column>
          <el-table-column prop="role" label="角色" width="120">
            <template #default="{ row }"><el-tag size="small">{{ row.role }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="department" label="部门" width="140" />
          <el-table-column label="状态" width="90" align="center">
            <template #default="{ row }">
              <el-tag :type="(row.status ?? 'active') === 'active' ? 'success' : 'danger'" size="small">
                {{ (row.status ?? 'active') === 'active' ? '正常' : '已禁用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="210" align="center">
            <template #default="{ row }">
              <el-button size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
              <el-button size="small" text :type="row.status === 'active' ? 'warning' : 'success'" @click="toggle(row)">
                {{ row.status === 'active' ? '禁用' : '启用' }}
              </el-button>
              <el-button size="small" text type="danger" @click="removeUser(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane v-if="canRoles" label="角色" name="roles">
        <el-table :data="roles" size="small" v-loading="loading">
          <el-table-column prop="name" label="角色" width="150">
            <template #default="{ row }">
              <el-tag size="small">{{ row.name }}</el-tag>
              <el-tag v-if="row.builtin" size="small" type="info" style="margin-left: 4px">内置</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="description" label="说明" min-width="150" />
          <el-table-column label="数据范围" width="110">
            <template #default="{ row }">
              <el-tag size="small" type="warning">{{ row.data_scope === 'all' ? '全站' : row.data_scope === 'department' ? '本部门' : '仅本人' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="Web 权限" min-width="220">
            <template #default="{ row }">
              <code style="font-size: 11.5px">{{ (row.permissions ?? []).join(', ') || '(无)' }}</code>
            </template>
          </el-table-column>
          <el-table-column label="可用工具" min-width="180">
            <template #default="{ row }">
              <code style="font-size: 11.5px">{{ (row.allowed_tools ?? []).join(', ') || '(无)' }}</code>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="150" align="center">
            <template #default="{ row }">
              <el-button size="small" text type="primary" :disabled="row.builtin" @click="openRoleEdit(row)">编辑</el-button>
              <el-button size="small" text type="danger" :disabled="row.builtin" @click="removeRole(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane v-if="canDepts" label="部门" name="departments">
        <el-table :data="departments" size="small" v-loading="loading">
          <el-table-column prop="name" label="部门" min-width="160" />
          <el-table-column prop="description" label="说明" min-width="180" />
          <el-table-column label="负责人" width="140">
            <template #default="{ row }">{{ row.manager_name || '—' }}</template>
          </el-table-column>
          <el-table-column label="成员数" width="90" align="center">
            <template #default="{ row }">{{ row.member_count ?? 0 }}</template>
          </el-table-column>
          <el-table-column label="操作" width="150" align="center">
            <template #default="{ row }">
              <el-button size="small" text type="primary" @click="openDeptEdit(row)">编辑</el-button>
              <el-button size="small" text type="danger" @click="removeDept(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- 新建用户 -->
    <el-dialog v-model="dialog" title="新建用户" width="460px">
      <el-form label-width="90px">
        <el-form-item label="工号/用户名"><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="显示名"><el-input v-model="form.display_name" placeholder="中文显示名(可选)" /></el-form-item>
        <el-form-item label="部门">
          <el-select v-model="form.department" filterable allow-create clearable style="width: 100%" placeholder="选择或输入部门">
            <el-option v-for="d in deptNames" :key="d" :label="d" :value="d" />
          </el-select>
        </el-form-item>
        <el-form-item label="密码"><el-input v-model="form.password" type="password" show-password /></el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role" style="width: 100%">
            <el-option v-for="r in roleNames" :key="r" :label="r" :value="r" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" @click="addUser">创建</el-button>
      </template>
    </el-dialog>

    <!-- 编辑用户 -->
    <el-dialog v-model="editDialog" title="编辑用户" width="460px">
      <el-form label-width="90px" v-if="editTarget">
        <el-form-item label="工号/用户名"><el-input :model-value="editTarget.name" disabled /></el-form-item>
        <el-form-item label="显示名"><el-input v-model="editForm.display_name" placeholder="中文显示名" /></el-form-item>
        <el-form-item label="部门">
          <el-select v-model="editForm.department" filterable allow-create clearable style="width: 100%" placeholder="选择或输入部门">
            <el-option v-for="d in deptNames" :key="d" :label="d" :value="d" />
          </el-select>
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="editForm.role" style="width: 100%">
            <el-option v-for="r in roleNames" :key="r" :label="r" :value="r" />
          </el-select>
        </el-form-item>
        <el-form-item label="重置密码"><el-input v-model="editForm.password" type="password" show-password placeholder="留空则不修改" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialog = false">取消</el-button>
        <el-button type="primary" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 角色 -->
    <el-dialog v-model="roleDialog" :title="roleEditing ? '编辑角色' : '新建角色'" width="620px">
      <el-form label-width="110px">
        <el-form-item label="角色名">
          <el-input v-model="roleForm.name" :disabled="roleEditing" placeholder="英文标识, 如 dept_manager" />
        </el-form-item>
        <el-form-item label="说明"><el-input v-model="roleForm.description" /></el-form-item>
        <el-form-item label="数据范围">
          <el-select v-model="roleForm.data_scope" style="width: 100%">
            <el-option v-for="s in catalog.data_scopes" :key="s.key" :label="`${s.label} — ${s.description ?? ''}`" :value="s.key" />
          </el-select>
        </el-form-item>
        <el-form-item label="Web 权限">
          <el-checkbox-group v-model="roleForm.permissions">
            <el-checkbox v-for="p in catalog.permissions" :key="p.key" :label="p.key">{{ p.label }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="可用工具">
          <el-input v-model="roleForm.allowed_tools" placeholder="逗号分隔；* 表示全部" />
        </el-form-item>
        <el-form-item label="可用子代理">
          <el-input v-model="roleForm.allowed_agents" placeholder="逗号分隔；* 表示全部" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="roleDialog = false">取消</el-button>
        <el-button type="primary" @click="saveRole">保存</el-button>
      </template>
    </el-dialog>

    <!-- 部门 -->
    <el-dialog v-model="deptDialog" :title="deptEditing ? '编辑部门' : '新建部门'" width="460px">
      <el-form label-width="90px">
        <el-form-item label="部门名">
          <el-input v-model="deptForm.name" :disabled="deptEditing" placeholder="如 设备运维部" />
        </el-form-item>
        <el-form-item label="说明"><el-input v-model="deptForm.description" /></el-form-item>
        <el-form-item label="负责人">
          <el-select v-model="deptForm.manager_id" filterable clearable style="width: 100%" placeholder="选择负责人(可选)">
            <el-option v-for="u in userOptions" :key="u.id" :label="u.name" :value="u.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="deptDialog = false">取消</el-button>
        <el-button type="primary" @click="saveDept">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>
