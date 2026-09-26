<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { fetchAttachmentObjectUrl } from '../api'

const props = defineProps<{ refId: string; url?: string; alt?: string }>()

const src = ref('')
let objUrl = ''

async function load() {
  if (objUrl) {
    URL.revokeObjectURL(objUrl)
    objUrl = ''
  }
  // 优先用后端返回的公开 URL（如 MinIO），否则用鉴权接口取 blob
  src.value = props.url || ''
  if (src.value) return
  try {
    objUrl = await fetchAttachmentObjectUrl(props.refId)
    src.value = objUrl
  } catch {
    src.value = ''
  }
}

watch(() => [props.refId, props.url], load, { immediate: true })
onBeforeUnmount(() => {
  if (objUrl) URL.revokeObjectURL(objUrl)
})
</script>

<template>
  <img v-if="src" :src="src" :alt="alt || refId" class="att-img" />
  <span v-else class="att-ph" :title="alt || refId">{{ alt || '图片' }}</span>
</template>

<style scoped>
.att-img { max-width: 180px; max-height: 140px; border-radius: 8px; border: 1px solid var(--border, #e5e7eb); display: block; }
.att-ph { display: inline-block; padding: 4px 8px; border-radius: 8px; font-size: 12px; background: var(--el-fill-color-light, #f2f3f5); color: var(--text-3, #999); }
</style>
