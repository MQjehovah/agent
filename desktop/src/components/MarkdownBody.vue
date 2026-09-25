<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { renderMarkdown } from '../utils/markdown'
import { renderMermaidIn } from '../utils/mermaid'

const props = defineProps<{ content: string }>()
const el = ref<HTMLElement | null>(null)

function refresh(): void {
  void nextTick(() => {
    if (el.value) void renderMermaidIn(el.value)
  })
}

watch(() => props.content, refresh)
onMounted(refresh)

/** 代码块"复制"按钮(事件代理, 避免为每块单独绑 handler) */
async function onClick(e: MouseEvent): Promise<void> {
  const target = e.target as HTMLElement
  if (!target.classList.contains('code-copy')) return
  const wrap = target.closest('.code-wrap')
  const code = wrap?.querySelector('code')?.textContent ?? ''
  try {
    await navigator.clipboard.writeText(code)
    target.textContent = '已复制'
    setTimeout(() => {
      target.textContent = '复制'
    }, 1500)
  } catch {
    ElMessage.warning('复制失败, 请手动选择复制')
  }
}
</script>

<template>
  <div ref="el" class="md" @click="onClick" v-html="renderMarkdown(props.content)" />
</template>
