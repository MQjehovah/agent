<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { agentApi } from '../api/agent'
import type { HistoryMessage } from '../api/types'
import {
  canContinueInDashboard,
  channelLabel,
  channelKindFromId,
  sessionArchiveKey,
  useSessionsStore,
  type SessionListItem
} from '../stores/sessions'
import { useChatStore } from '../stores/chat'
import { useRouter } from 'vue-router'
import { renderMarkdown } from '../utils/markdown'

const router = useRouter()
const chat = useChatStore()
const sessionsStore = useSessionsStore()

/** 显示已归档会话(默认隐藏, 与侧栏「已归档」分组口径一致) */
const showArchived = ref(false)
const archivedKeySet = computed(() => sessionsStore.archivedKeySet)
const tableData = computed(() =>
  showArchived.value ? sessionsStore.sessions : sessionsStore.activeSessions
)
function isArchived(row: SessionListItem): boolean {
  return archivedKeySet.value.has(sessionArchiveKey(row.mode, row.id))
}

/** 行渠道细类(优先服务端 channel_kind, 缺失按 ID 前缀兜底) */
function rowChannelKind(row: SessionListItem): string {
  return row.channelKind || (row.mode === 'local' ? 'local' : channelKindFromId(row.id))
}
function rowChannelLabel(row: SessionListItem): string {
  return channelLabel(rowChannelKind(row))
}
/** 渠道标签颜色 */
function rowChannelTagType(row: SessionListItem): 'primary' | 'success' | 'warning' | 'info' {
  switch (rowChannelKind(row)) {
    case 'web':
      return 'primary'
    case 'dingtalk':
      return 'success'
    case 'dingtalk_group':
      return 'warning'
    default:
      return 'info'
  }
}
/** 是否可在 dashboard 内续聊(仅 web/本地; 钉钉会话只读) */
function rowCanContinue(row: SessionListItem): boolean {
  return canContinueInDashboard(row)
}

const viewVisible = ref(false)
const viewSessionId = ref('')
const viewMessages = ref<HistoryMessage[]>([])
const viewLoading = ref(false)

async function refresh() {
  try {
    await sessionsStore.refresh()
  } catch (err) {
    ElMessage.error(`加载会话失败:${(err as Error).message}`)
  }
}

/** 查看会话消息:agent 走 HTTP,本地走 IPC */
async function openSession(s: SessionListItem) {
  viewSessionId.value = s.title
  viewVisible.value = true
  viewLoading.value = true
  try {
    if (s.mode === 'local') {
      const stored = await window.desktop.invoke<Array<{ role: string; content: string }>>('localagent:messages', s.id)
      // 轨迹回填消息不在查看视图里展示
      viewMessages.value = (stored ?? []).filter((m) => m.role !== 'tool')
    } else {
      const res = await agentApi.sessionMessages(s.id)
      viewMessages.value = res.messages ?? []
    }
  } catch (err) {
    ElMessage.error(`加载消息失败:${(err as Error).message}`)
  } finally {
    viewLoading.value = false
  }
}

/** 继续对话:本地会话切到本地模式并回放历史 */
function continueSession(s: SessionListItem) {
  if (chat.streaming) return
  if (!canContinueInDashboard(s)) {
    ElMessage.info('该会话来自钉钉等外部渠道，仅支持查看历史，请在对应渠道继续对话')
    return
  }
  if (s.mode === 'local') {
    chat
      .loadLocalMessages(s.id, { workspace: s.workspace, ephemeral: s.ephemeral })
      .then(() => void router.push('/chat'))
      .catch((err: Error) => ElMessage.error(`加载消息失败:${err.message}`))
    return
  }
  void agentApi
    .sessionMessages(s.id)
    .then((res) => {
      chat.loadHistory(s.id, res.messages ?? [])
      void router.push('/chat')
    })
    .catch((err) => ElMessage.error(`加载消息失败:${(err as Error).message}`))
}

async function removeSession(s: SessionListItem) {
  try {
    await ElMessageBox.confirm(`确认删除会话 ${s.title}?该操作不可恢复。`, '删除会话', { type: 'warning' })
  } catch {
    return
  }
  try {
    if (chat.sessionId === s.id) chat.newSession()
    await sessionsStore.remove(s.id, s.mode)
    ElMessage.success('已删除')
  } catch (err) {
    ElMessage.error(`删除失败:${(err as Error).message}`)
  }
}

function formatTime(t: string | number): string {
  const d = new Date(t)
  return Number.isNaN(d.getTime()) ? String(t) : d.toLocaleString()
}

onMounted(refresh)
</script>

<template>
  <div class="sessions-view">
    <header class="view-header">
      <span class="view-title">会话历史</span>
      <el-checkbox v-model="showArchived" class="view-archived-toggle">显示已归档</el-checkbox>
      <el-button :icon="Refresh" size="small" :loading="sessionsStore.loading" @click="refresh">刷新</el-button>
    </header>

    <el-table
      :data="tableData"
      :row-class-name="({ row }: { row: SessionListItem }) => (isArchived(row) ? 'row-archived' : '')"
      v-loading="sessionsStore.loading"
      height="100%"
      empty-text="暂无会话"
    >
      <el-table-column label="会话" min-width="240" show-overflow-tooltip>
        <template #default="{ row }">
          <span>{{ row.title }}</span>
          <el-tag v-if="row.mode === 'local'" size="small" type="success" class="session-tag">本地</el-tag>
          <el-tag
            v-if="row.ephemeral"
            size="small"
            type="warning"
            class="session-tag"
            title="退出后自动删除，不落历史"
          >临时</el-tag>
          <el-tag v-if="isArchived(row)" size="small" type="info" class="session-tag">已归档</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="渠道" width="110" align="center">
        <template #default="{ row }">
          <el-tag size="small" effect="plain" :type="rowChannelTagType(row)">{{ rowChannelLabel(row) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="180">
        <template #default="{ row }">{{ formatTime(row.createdAt) }}</template>
      </el-table-column>
      <el-table-column label="消息数" width="90" align="center">
        <template #default="{ row }">{{ row.messageCount ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="状态" width="90" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.isStreaming" type="warning" size="small">进行中</el-tag>
          <el-tag v-else type="info" size="small">空闲</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="210" align="center">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="openSession(row)">查看</el-button>
          <el-tooltip
            :disabled="rowCanContinue(row)"
            content="钉钉等外部渠道仅支持查看历史，请在对应渠道继续对话"
            placement="top"
          >
            <span>
              <el-button
                size="small"
                text
                type="primary"
                :disabled="!rowCanContinue(row)"
                @click="continueSession(row)"
              >继续对话</el-button>
            </span>
          </el-tooltip>
          <el-button size="small" text type="danger" @click="removeSession(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="viewVisible" :title="`会话 ${viewSessionId}`" width="720px" top="6vh">
      <div v-loading="viewLoading" class="session-messages">
        <div v-for="(m, i) in viewMessages" :key="i" class="msg-row" :class="m.role === 'user' ? 'user' : 'assistant'">
          <div v-if="m.role === 'user'" class="user-bubble">{{ m.content }}</div>
          <div v-else class="assistant-text md" v-html="renderMarkdown(m.content)" />
        </div>
        <el-empty v-if="!viewLoading && viewMessages.length === 0" description="无消息" />
      </div>
    </el-dialog>
  </div>
</template>
