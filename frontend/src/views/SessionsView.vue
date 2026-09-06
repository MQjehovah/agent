<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, del } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

interface SessionRow { id: string; created_at: string; message_count: number; is_streaming: boolean }
interface HistoryMsg { role: string; content: string }

const sessions = ref<SessionRow[]>([])
const loading = ref(false)
const viewVisible = ref(false)
const viewId = ref('')
const viewMessages = ref<HistoryMsg[]>([])

async function load() {
  loading.value = true
  try {
    const d = await api<{ sessions: SessionRow[] }>('/api/sessions')
    sessions.value = d.sessions ?? []
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

async function view(row: SessionRow) {
  viewId.value = row.id
  viewVisible.value = true
  try {
    const d = await api<{ messages: HistoryMsg[] }>(`/api/sessions/${encodeURIComponent(row.id)}/messages`)
    viewMessages.value = d.messages ?? []
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function remove(row: SessionRow) {
  try {
    await ElMessageBox.confirm(`删除会话 ${row.id}?`, '确认', { type: 'warning' })
  } catch { return }
  try {
    await del(`/api/sessions/${encodeURIComponent(row.id)}`)
    ElMessage.success('已删除')
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
      <div><h2>会话</h2><div class="sub">与 agent 的历史对话记录</div></div>
      <el-button @click="load">刷新</el-button>
    </div>
    <el-table :data="sessions" v-loading="loading" empty-text="暂无会话">
      <el-table-column prop="id" label="会话 ID" min-width="220" show-overflow-tooltip />
      <el-table-column label="创建时间" width="180">
        <template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template>
      </el-table-column>
      <el-table-column prop="message_count" label="消息数" width="90" align="center" />
      <el-table-column label="状态" width="100" align="center">
        <template #default="{ row }">
          <el-tag :type="row.is_streaming ? 'warning' : 'info'" size="small">{{ row.is_streaming ? '进行中' : '空闲' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="160" align="center">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="view(row)">查看</el-button>
          <el-button size="small" text type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="viewVisible" :title="`会话 ${viewId}`" width="760px" top="6vh">
      <div style="max-height: 60vh; overflow-y: auto">
        <div v-for="(m, i) in viewMessages" :key="i" class="msg" :class="m.role === 'user' ? 'user' : 'assistant'">
          <div class="bubble" :style="m.role !== 'user' ? 'white-space:pre-wrap' : ''">{{ m.content }}</div>
        </div>
        <el-empty v-if="viewMessages.length === 0" description="无消息" />
      </div>
    </el-dialog>
  </div>
</template>
