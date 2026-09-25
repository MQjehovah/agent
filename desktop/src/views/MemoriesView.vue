<script setup lang="ts">
defineOptions({ name: 'MemoriesView' })
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { request } from '../api/client'
import { useSettingsStore } from '../stores/settings'

/** 记忆管理:个人私有记忆(admin 可删公共记忆)。 */
interface Memory { id: string; content: string; category: string; scope?: string; owner_id?: string; created_at?: string }

const settings = useSettingsStore()
const isAdmin = computed(() => settings.user?.role === 'admin')

const memories = ref<Memory[]>([])
const total = ref(0)
const loading = ref(false)
const keyword = ref('')

async function load(): Promise<void> {
  loading.value = true
  try {
    const d = await request<{ memories: Memory[]; total: number }>(
      'agent',
      `/api/memories?q=${encodeURIComponent(keyword.value)}&limit=200`
    )
    memories.value = d.memories ?? []
    total.value = d.total ?? 0
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

function scopeTag(m: Memory): { label: string; type: 'primary' | 'success' } {
  return m.scope === 'user' ? { label: '个人', type: 'success' } : { label: '公共', type: 'primary' }
}

function canDelete(m: Memory): boolean {
  return isAdmin.value || m.scope === 'user'
}

async function remove(m: Memory): Promise<void> {
  try {
    await ElMessageBox.confirm('删除这条记忆?', '确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    await request('agent', `/api/memories/${m.id}`, { method: 'DELETE' })
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

onMounted(load)
</script>

<template>
  <div class="sessions-view">
    <header class="view-header">
      <span class="view-title">记忆管理</span>
      <div class="head-actions">
        <el-input v-model="keyword" placeholder="搜索记忆" clearable style="width: 240px" @keyup.enter="load" />
        <el-button :icon="Search" size="small" @click="load">搜索</el-button>
      </div>
    </header>

    <div v-loading="loading" class="mem-body">
      <div
        v-for="m in memories"
        :key="m.id"
        class="mem-row"
      >
        <el-tag size="small">{{ m.category || '未分类' }}</el-tag>
        <el-tag size="small" :type="scopeTag(m).type" effect="plain">{{ scopeTag(m).label }}</el-tag>
        <span class="mem-content">{{ m.content }}</span>
        <el-button v-if="canDelete(m)" size="small" text type="danger" @click="remove(m)">删除</el-button>
      </div>
      <el-empty v-if="!loading && memories.length === 0" :description="keyword ? '无匹配记忆' : '暂无记忆'" />
      <div v-if="!loading && memories.length > 0" class="mem-count">共 {{ total }} 条</div>
    </div>
  </div>
</template>

<style scoped>
.head-actions { display: flex; align-items: center; gap: 8px; }
.mem-body { flex: 1; min-height: 0; overflow-y: auto; padding: 10px 18px; }
.mem-row {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 9px 2px; border-bottom: 1px solid var(--el-border-color-lighter);
}
.mem-content { flex: 1; min-width: 0; word-break: break-word; line-height: 1.6; }
.mem-count { padding: 6px 2px; font-size: 12px; color: var(--el-text-color-secondary); }
</style>
