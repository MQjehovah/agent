<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, del, post } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

interface Memory { id: string; content: string; category: string; scope?: string; created_at?: string }
interface Proposal { id: string; content?: string; category?: string; status?: string }

const memories = ref<Memory[]>([])
const total = ref(0)
const loading = ref(false)
const keyword = ref('')
const proposals = ref<Proposal[]>([])

async function load() {
  loading.value = true
  try {
    const d = await api<{ memories: Memory[]; total: number }>(`/api/memories?q=${encodeURIComponent(keyword.value)}&limit=200`)
    memories.value = d.memories ?? []
    total.value = d.total ?? 0
    try {
      const p = await api<{ proposals?: Proposal[] }>('/api/memory/proposals?status=pending')
      proposals.value = p.proposals ?? []
    } catch {
      proposals.value = []
    }
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
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

async function approve(p: Proposal, approveIt: boolean) {
  try {
    await post(`/api/memory/proposals/${p.id}`, { action: approveIt ? 'approve' : 'reject' })
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
      <h2>记忆(共 {{ total }} 条)</h2>
      <div style="display: flex; gap: 8px">
        <el-input v-model="keyword" placeholder="搜索记忆" clearable style="width: 240px" @keyup.enter="load" />
        <el-button @click="load">搜索</el-button>
      </div>
    </div>

    <template v-if="proposals.length">
      <h3 style="font-size: 14px">待审批的记忆提案</h3>
      <div v-for="p in proposals" :key="p.id" style="border: 1px dashed var(--el-border-color); border-radius: 8px; padding: 8px 12px; margin-bottom: 8px; display: flex; align-items: center; gap: 10px">
        <span style="flex: 1">{{ p.content }}</span>
        <el-button size="small" type="success" @click="approve(p, true)">采纳</el-button>
        <el-button size="small" @click="approve(p, false)">拒绝</el-button>
      </div>
    </template>

    <div v-loading="loading" style="margin-top: 10px">
      <div v-for="m in memories" :key="m.id" style="border-bottom: 1px solid var(--el-border-color-lighter); padding: 9px 2px; display: flex; gap: 10px; align-items: flex-start">
        <el-tag size="small">{{ m.category || '未分类' }}</el-tag>
        <span style="flex: 1">{{ m.content }}</span>
        <el-button size="small" text type="danger" @click="remove(m)">删除</el-button>
      </div>
      <el-empty v-if="!loading && memories.length === 0" description="无记忆" />
    </div>
  </div>
</template>
