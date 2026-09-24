<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, del, patch } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

interface Installation {
  id?: number
  capability_id: string
  capability_name: string
  kind: string
  enabled: boolean
  installed_at?: string
  updated_at?: string
}

const router = useRouter()
const rows = ref<Installation[]>([])
const loading = ref(false)

function kindLabel(kind: string): string {
  const map: Record<string, string> = { mcp: '连接器', skill: '技能', agent: '专家', plugin: '能力包' }
  return map[kind] ?? (kind || '未知')
}

async function load() {
  loading.value = true
  try {
    const d = await api<{ installations: Installation[] }>('/api/market/installations')
    rows.value = d.installations ?? []
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

async function toggle(row: Installation, value: string | number | boolean) {
  const enabled = Boolean(value)
  try {
    await patch(`/api/market/installations/${row.capability_id}`, { enabled })
    ElMessage.success(enabled ? '已启用' : '已停用')
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
  await load()
}

async function uninstall(row: Installation) {
  try {
    await ElMessageBox.confirm(`确认卸载「${row.capability_name}」? 卸载后工具将立即下线。`,
      '卸载确认', { type: 'warning' })
  } catch { return }
  try {
    await del(`/api/market/installations/${row.capability_id}`)
    ElMessage.success('已卸载')
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
      <div>
        <h2>我的连接器</h2>
        <div class="sub">云端托管：由平台网关调用，无需本机进程；停用后该连接器工具立即从对话中下线</div>
      </div>
      <div class="actions">
        <el-button @click="load">刷新</el-button>
        <el-button type="primary" @click="router.push('/market')">去能力市场</el-button>
      </div>
    </div>

    <div v-loading="loading">
      <el-table v-if="rows.length" :data="rows" stripe>
        <el-table-column label="名称" prop="capability_name" min-width="180" />
        <el-table-column label="类型" width="120">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ kindLabel(row.kind) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="安装时间" width="200">
          <template #default="{ row }">
            <span class="muted">{{ (row.installed_at || '').replace('T', ' ').slice(0, 19) || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="140">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled" inline-prompt active-text="启用" inactive-text="停用"
                       @change="(v: string | number | boolean) => toggle(row, v)" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button size="small" type="danger" plain @click="uninstall(row)">卸载</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-if="!loading && rows.length === 0"
                description="还没有云端托管连接器，去能力市场安装一个吧">
        <el-button type="primary" @click="router.push('/market')">去能力市场</el-button>
      </el-empty>
    </div>
  </div>
</template>

<style scoped>
.muted { color: var(--text-3); font-size: 12px; }
</style>
