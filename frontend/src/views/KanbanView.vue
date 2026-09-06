<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, del, patch, post } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

interface Task {
  id: string
  title: string
  description?: string
  priority: number
  column: string
  assignee?: string
  tags?: string[] | string
  source?: string
}

const COLUMNS = [
  { key: 'backlog', title: '待规划' },
  { key: 'todo', title: '待办' },
  { key: 'in_progress', title: '进行中' },
  { key: 'done', title: '已完成' }
]

const tasks = ref<Task[]>([])
const loading = ref(false)
const addVisible = ref(false)
const form = ref({ title: '', description: '', priority: 3, column: 'backlog' })

const byColumn = computed(() => {
  const map: Record<string, Task[]> = {}
  for (const c of COLUMNS) map[c.key] = []
  for (const t of tasks.value) (map[t.column] ??= []).push(t)
  return map
})

async function load() {
  loading.value = true
  try {
    const d = await api<{ tasks: Task[] }>('/api/kanban')
    tasks.value = d.tasks ?? []
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

async function add() {
  if (!form.value.title.trim()) return
  try {
    await post('/api/kanban', form.value)
    addVisible.value = false
    form.value = { title: '', description: '', priority: 3, column: 'backlog' }
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function move(task: Task, column: string) {
  if (task.column === column) return
  try {
    await post(`/api/kanban/${task.id}/move`, { column })
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function remove(task: Task) {
  try {
    await ElMessageBox.confirm(`删除任务「${task.title}」?`, '确认', { type: 'warning' })
  } catch { return }
  try {
    await del(`/api/kanban/${task.id}`)
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

onMounted(load)
</script>

<template>
  <div class="page" style="display: flex; flex-direction: column">
    <div class="page-head">
      <h2>看板</h2>
      <div>
        <el-button @click="load">刷新</el-button>
        <el-button type="primary" @click="addVisible = true">新建任务</el-button>
      </div>
    </div>

    <div style="display: flex; gap: 12px; flex: 1; min-height: 0">
      <div v-for="c in COLUMNS" :key="c.key" style="flex: 1; background: var(--el-bg-color-page); border-radius: 10px; padding: 10px; overflow-y: auto">
        <div style="font-weight: 600; margin-bottom: 8px">{{ c.title }} <el-tag size="small">{{ (byColumn[c.key] ?? []).length }}</el-tag></div>
        <div v-for="t in byColumn[c.key]" :key="t.id" style="background: var(--el-bg-color); border: 1px solid var(--el-border-color-lighter); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px">
          <div style="font-weight: 600">{{ t.title }}</div>
          <div v-if="t.description" style="font-size: 12px; color: var(--el-text-color-secondary); margin: 4px 0">{{ t.description }}</div>
          <div style="display: flex; align-items: center; gap: 6px; margin-top: 6px">
            <el-tag size="small" :type="t.priority <= 1 ? 'danger' : t.priority === 2 ? 'warning' : 'info'">P{{ t.priority }}</el-tag>
            <span v-if="t.assignee" style="font-size: 12px; color: var(--el-text-color-secondary)">{{ t.assignee }}</span>
            <el-dropdown trigger="click" size="small" @command="(col: string) => move(t, col)">
              <el-button size="small" text type="primary">移动</el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item v-for="cc in COLUMNS" :key="cc.key" :command="cc.key" :disabled="cc.key === t.column">{{ cc.title }}</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-button size="small" text type="danger" style="margin-left: auto" @click="remove(t)">删除</el-button>
          </div>
        </div>
      </div>
    </div>

    <el-dialog v-model="addVisible" title="新建看板任务" width="440px">
      <el-form label-width="70px">
        <el-form-item label="标题"><el-input v-model="form.title" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="form.description" type="textarea" :rows="2" /></el-form-item>
        <el-form-item label="优先级"><el-input-number v-model="form.priority" :min="1" :max="5" /></el-form-item>
        <el-form-item label="列">
          <el-select v-model="form.column">
            <el-option v-for="c in COLUMNS" :key="c.key" :label="c.title" :value="c.key" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addVisible = false">取消</el-button>
        <el-button type="primary" @click="add">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>
