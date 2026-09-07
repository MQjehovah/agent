<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../../api'
import { Connection, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

interface WTask { task_id: string; status: string; created_at: string; error?: string }

const tasks = ref<WTask[]>([])
const loading = ref(false)
const detailVisible = ref(false)
const detail = ref<any>({})
const detailId = ref('')
let timer: number | undefined

async function load() {
  loading.value = true
  try {
    const d = await api<{ tasks: WTask[] }>('/webhook/tasks?limit=100')
    tasks.value = d.tasks ?? []
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

async function openDetail(t: WTask) {
  detailId.value = t.task_id
  detailVisible.value = true
  try {
    detail.value = await api(`/webhook/execute/${encodeURIComponent(t.task_id)}/result`)
  } catch (e) {
    detail.value = { status: '未知', error: (e as Error).message }
  }
}

function statusType(s: string) {
  return s === 'completed' ? 'success' : s === 'failed' ? 'danger' : s === 'running' ? 'warning' : 'info'
}

onMounted(() => {
  void load()
  timer = window.setInterval(() => void load(), 8000)
})
onBeforeUnmount(() => { if (timer) window.clearInterval(timer) })
</script>

<template>
  <div>
    <div class="bar">
      <span class="hint">状态为内存态，重启后清空，每 8s 自动刷新</span>
      <el-button size="small" :icon="Refresh" @click="load">刷新</el-button>
    </div>

    <el-table :data="tasks" v-loading="loading" empty-text="暂无任务">
      <el-table-column prop="task_id" label="任务 ID" min-width="280" class-name="mono" />
      <el-table-column label="状态" width="120" align="center">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType(row.status)">{{ row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="提交时间" width="190">
        <template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template>
      </el-table-column>
      <el-table-column label="操作" width="120" align="center">
        <template #default="{ row }">
          <el-button size="small" text type="primary" :icon="Connection" @click="openDetail(row)">详情</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="detailVisible" :title="`任务 ${detailId.slice(0, 18)}…`" width="640px" top="6vh">
      <el-descriptions :column="1" border>
        <el-descriptions-item label="任务 ID"><span class="mono">{{ detail.task_id }}</span></el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag size="small" :type="statusType(detail.status ?? '')">{{ detail.status }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="错误"><span v-if="detail.error">{{ detail.error }}</span><span v-else>—</span></el-descriptions-item>
        <el-descriptions-item label="结果">
          <pre class="mono" style="margin: 0; white-space: pre-wrap; word-break: break-word; max-height: 320px; overflow: auto">{{ detail.result ?? '—' }}</pre>
        </el-descriptions-item>
      </el-descriptions>
    </el-dialog>
  </div>
</template>

<style scoped>
.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.hint {
  font-size: 12px;
  color: var(--text-3);
}
</style>
