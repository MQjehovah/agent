<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api, del, isAdmin } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

interface Memory { id: string; content: string; category: string; scope?: string; owner_id?: string; created_at?: string }

const route = useRoute()
const viewAll = computed(() => route.query.view === 'all')
const admin = isAdmin()
const memories = ref<Memory[]>([])
const total = ref(0)
const loading = ref(false)
const keyword = ref('')

async function load() {
  loading.value = true
  try {
    const qs = `/api/memories?q=${encodeURIComponent(keyword.value)}&limit=200${viewAll.value ? '&view=all' : ''}`
    const d = await api<{ memories: Memory[]; total: number }>(qs)
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
  return admin || m.scope === 'user'
}

async function remove(m: Memory) {
  try {
    await ElMessageBox.confirm('删除这条记忆?', '确认', { type: 'warning' })
  } catch { return }
  try {
    await del(`/api/memories/${m.id}`)
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
      <div><h2>记忆管理</h2>
        <div class="sub">{{ viewAll ? '全部用户私有 + 全局公共记忆（运维全量）' : '仅展示我的私有记忆（个人空间，不含全局公共记忆）' }}</div>
      </div>
      <div style="display: flex; gap: 8px">
        <el-input v-model="keyword" placeholder="搜索记忆" clearable style="width: 240px" @keyup.enter="load" />
        <el-button @click="load">搜索</el-button>
      </div>
    </div>

    <div v-loading="loading">
      <div v-for="m in memories" :key="m.id" style="border-bottom: 1px solid var(--el-border-color-lighter); padding: 9px 2px; display: flex; gap: 10px; align-items: flex-start">
        <el-tag size="small">{{ m.category || '未分类' }}</el-tag>
        <el-tag size="small" :type="scopeTag(m).type" effect="plain">{{ scopeTag(m).label }}</el-tag>
        <el-tag v-if="viewAll && m.scope === 'user'" size="small" effect="plain" type="info">{{ m.owner_id || '?' }}</el-tag>
        <span style="flex: 1">{{ m.content }}</span>
        <el-button v-if="canDelete(m)" size="small" text type="danger" @click="remove(m)">删除</el-button>
      </div>
      <div v-if="!loading && memories.length === 0" style="margin-top: 10px">
        <el-empty :description="keyword ? '无匹配记忆' : '暂无记忆'" />
      </div>
      <div v-if="!loading && memories.length > 0" style="font-size: 12px; color: var(--el-text-color-secondary); padding: 6px 2px">共 {{ total }} 条</div>
    </div>
  </div>
</template>
