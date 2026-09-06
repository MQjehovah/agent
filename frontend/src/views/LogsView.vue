<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { getToken } from '../api'

const lines = ref<string[]>([])
const connected = ref(false)
let es: EventSource | null = null

function connect() {
  // logs/stream 为 GET SSE:带 token 用查询参数(EventSource 无法带 header),与旧版一致
  es = new EventSource(`/api/logs/stream?token=${encodeURIComponent(getToken())}`)
  es.onopen = () => (connected.value = true)
  es.onerror = () => (connected.value = false)
  es.onmessage = (ev) => {
    lines.value.push(ev.data)
    if (lines.value.length > 500) lines.value.splice(0, lines.value.length - 500)
  }
}

onMounted(connect)
onBeforeUnmount(() => es?.close())
</script>

<template>
  <div class="page" style="display: flex; flex-direction: column">
    <div class="page-head">
      <h2>运行日志</h2>
      <div style="display: flex; align-items: center; gap: 8px">
        <el-tag :type="connected ? 'success' : 'danger'" size="small">{{ connected ? '已连接' : '未连接' }}</el-tag>
        <el-button size="small" @click="lines = []">清空</el-button>
      </div>
    </div>
    <div style="flex: 1; overflow-y: auto; background: #111; border-radius: 8px; padding: 10px 12px; font-family: Consolas, monospace; font-size: 12px; line-height: 1.7; color: #d0d0d0">
      <div v-for="(l, i) in lines" :key="i" style="white-space: pre-wrap; word-break: break-all">{{ l }}</div>
      <div v-if="lines.length === 0" style="color: #666">等待日志输出…</div>
    </div>
  </div>
</template>
