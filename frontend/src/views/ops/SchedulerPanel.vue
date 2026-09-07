<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, del, patch, post } from '../../api'
import { ElMessage, ElMessageBox } from 'element-plus'

interface Task {
  id: string
  name: string
  cron: string
  task: string
  enabled: boolean
  static: boolean
  user_id?: string
  user_name?: string
  run_count: number
  last_run_at?: string
  last_result?: string
  last_error?: string
}

const props = withDefaults(defineProps<{ scopeAll?: boolean }>(), { scopeAll: false })

const tasks = ref<Task[]>([])
const loading = ref(false)
const dialog = ref(false)
const editing = ref<Task | null>(null)
const form = ref({ name: '', cron: '', task: '' })

async function load() {
  loading.value = true
  try {
    const qs = props.scopeAll ? '?scope=all' : ''
    const d = await api<{ tasks: Task[] }>(`/api/scheduler/tasks${qs}`)
    tasks.value = d.tasks ?? []
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

function openAdd() {
  editing.value = null
  form.value = { name: '', cron: '', task: '' }
  dialog.value = true
}

function openEdit(t: Task) {
  editing.value = t
  form.value = { name: t.name, cron: t.cron, task: t.task }
  dialog.value = true
}

async function save() {
  try {
    if (editing.value) await patch(`/api/scheduler/tasks/${editing.value.id}`, form.value)
    else await post('/api/scheduler/tasks', form.value)
    dialog.value = false
    ElMessage.success('已保存')
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function toggle(t: Task) {
  try {
    await patch(`/api/scheduler/tasks/${t.id}`, { enabled: !t.enabled })
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function remove(t: Task) {
  try {
    await ElMessageBox.confirm(`删除定时任务「${t.name}」?`, '确认', { type: 'warning' })
  } catch { return }
  try {
    await del(`/api/scheduler/tasks/${t.id}`)
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="bar">
      <el-button @click="load">刷新</el-button>
      <el-button type="primary" @click="openAdd">新建</el-button>
    </div>

    <el-table :data="tasks" v-loading="loading" empty-text="暂无定时任务">
      <el-table-column prop="name" label="名称" min-width="140" />
      <el-table-column label="类型" width="96" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.static" type="warning" size="small" effect="plain">系统级</el-tag>
          <el-tag v-else type="info" size="small" effect="plain">个人</el-tag>
        </template>
      </el-table-column>
      <el-table-column v-if="scopeAll" label="归属" width="140" show-overflow-tooltip>
        <template #default="{ row }">{{ row.user_name || row.user_id || '系统' }}</template>
      </el-table-column>
      <el-table-column prop="cron" label="Cron" width="140">
        <template #default="{ row }"><code>{{ row.cron }}</code></template>
      </el-table-column>
      <el-table-column prop="task" label="任务内容" min-width="240" show-overflow-tooltip />
      <el-table-column label="状态" width="90" align="center">
        <template #default="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'info'" size="small">{{ row.enabled ? '启用' : '停用' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="run_count" label="运行次数" width="90" align="center" />
      <el-table-column label="上次结果" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ row.last_error || row.last_result || '—' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="200" align="center">
        <template #default="{ row }">
          <el-button size="small" text type="primary" :disabled="row.static" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" text :type="row.enabled ? 'warning' : 'success'" :disabled="row.static" @click="toggle(row)">
            {{ row.enabled ? '停用' : '启用' }}
          </el-button>
          <el-button size="small" text type="danger" :disabled="row.static" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialog" :title="editing ? '编辑定时任务' : '新建定时任务'" width="480px">
      <el-form label-width="80px">
        <el-form-item label="名称"><el-input v-model="form.name" placeholder="未命名" /></el-form-item>
        <el-form-item label="Cron"><el-input v-model="form.cron" placeholder="0 9 * * 1-5(工作日 9 点)" /></el-form-item>
        <el-form-item label="任务"><el-input v-model="form.task" type="textarea" :rows="3" placeholder="要执行的任务描述" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.bar {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  margin-bottom: 10px;
}
</style>
