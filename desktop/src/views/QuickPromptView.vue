<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import logoUrl from '../assets/logo.svg'

/** 快速提问小窗(#/quick):输入后 Enter 提交 → 主窗口新建会话并发送 */
const text = ref('')
const submitting = ref(false)
/** 已受理(等待主窗口处理结果):锁输入避免重复提交 */
const accepted = ref(false)
const error = ref('')
const inputRef = ref<HTMLInputElement | null>(null)
/** 主进程错误回执订阅(发送失败/流式中/取消选目录时保留窗口与输入) */
let offQuickError: (() => void) | null = null

function focusInput(): void {
  inputRef.value?.focus()
}

async function submit(): Promise<void> {
  const value = text.value.trim()
  if (!value || submitting.value || accepted.value) return
  submitting.value = true
  error.value = ''
  try {
    // 受理失败(未登录等)立即回显;受理成功后主窗口处理,成功会关窗、失败走 quick:error
    const res = await window.desktop.invoke<{ ok: boolean; error?: string }>('quick:submit', { text: value })
    if (res?.ok) accepted.value = true
    else error.value = res?.error || '发送失败,请重试'
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    submitting.value = false
  }
}

async function close(): Promise<void> {
  await window.desktop.invoke('quick:close')
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    e.preventDefault()
    void close()
    return
  }
  if (e.key === 'Enter' && !e.isComposing) {
    e.preventDefault()
    void submit()
  }
}

onMounted(() => {
  focusInput()
  // 兜底:焦点不在输入框时 Esc 也能关闭
  window.addEventListener('keydown', onKeydown)
  offQuickError = window.desktop.onQuickError((payload) => {
    // 处理失败:解锁输入并回显原因,输入内容原样保留
    accepted.value = false
    error.value = payload.message || '发送失败,请重试'
    focusInput()
  })
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  offQuickError?.()
})
</script>

<template>
  <div class="quick">
    <img class="quick-logo" :src="logoUrl" alt="" />
    <input
      ref="inputRef"
      v-model="text"
      class="quick-input"
      type="text"
      placeholder="向零号员工提问…"
      :disabled="submitting || accepted"
      @input="error = ''"
      @keydown="onKeydown"
    />
    <span v-if="error" class="quick-error">{{ error }}</span>
    <span v-else-if="accepted" class="quick-hint">已受理,处理中…</span>
    <span v-else class="quick-hint">Enter 发送 · Esc 关闭</span>
  </div>
</template>

<style scoped>
.quick {
  height: 100%;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 18px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color);
  box-sizing: border-box;
}

.quick-logo {
  width: 26px;
  height: 26px;
  flex: none;
  border-radius: 7px;
  object-fit: contain;
}

.quick-input {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  color: var(--el-text-color-primary);
  font-size: 15px;
  font-family: inherit;
  line-height: 1.5;
}

.quick-input::placeholder {
  color: var(--el-text-color-secondary);
}

.quick-input:disabled {
  opacity: 0.6;
}

.quick-hint {
  flex: none;
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
  user-select: none;
}

.quick-error {
  flex: none;
  max-width: 200px;
  font-size: 11.5px;
  color: var(--el-color-danger);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
