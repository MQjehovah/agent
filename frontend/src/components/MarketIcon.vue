<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { fetchBlobUrl } from '../api'

const props = defineProps<{
  id: string
  name?: string
  size?: number
  hasIcon?: boolean
  letter?: string
  color?: string
}>()

const src = ref('')
let objUrl = ''

async function load() {
  if (objUrl) {
    URL.revokeObjectURL(objUrl)
    objUrl = ''
  }
  src.value = ''
  if (!props.id || props.hasIcon === false) return
  try {
    objUrl = await fetchBlobUrl(`/api/market/capabilities/${encodeURIComponent(props.id)}/icon`)
    src.value = objUrl
  } catch {
    src.value = ''
  }
}

watch(() => [props.id, props.hasIcon], load, { immediate: true })
onBeforeUnmount(() => {
  if (objUrl) URL.revokeObjectURL(objUrl)
})

const initial = (): string =>
  (props.letter || props.name || '?').trim().slice(0, 1).toUpperCase() || '?'

const boxStyle = () => {
  const base: Record<string, string> = {}
  if (props.size) {
    base.width = props.size + 'px'
    base.height = props.size + 'px'
  }
  return base
}
const phStyle = () => {
  const s: Record<string, string> = { ...boxStyle() }
  if (props.color) {
    s.color = props.color
    s.borderColor = props.color + '55'
    s.background = props.color + '14'
  }
  if (props.size) s.fontSize = Math.max(12, Math.round(props.size * 0.4)) + 'px'
  return s
}
</script>

<template>
  <img
    v-if="src"
    class="cap-icon"
    :src="src"
    :alt="name || '能力'"
    :style="boxStyle()"
  />
  <span v-else class="cap-icon cap-icon-ph" :style="phStyle()">
    {{ initial() }}
  </span>
</template>

<style scoped>
.cap-icon {
  width: 24px;
  height: 24px;
  flex: none;
  border-radius: 6px;
  object-fit: cover;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  vertical-align: middle;
}
.cap-icon-ph {
  background: var(--el-fill-color-light, #f2f3f5);
  color: var(--el-text-color-secondary, #909399);
  font-size: 12px;
  font-weight: 600;
}
</style>
