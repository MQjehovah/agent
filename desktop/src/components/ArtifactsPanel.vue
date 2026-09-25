<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Close, Document, FolderOpened, Refresh } from '@element-plus/icons-vue'
import { useChatStore } from '../stores/chat'
import MarkdownBody from './MarkdownBody.vue'

/**
 * 对话产物侧边栏(Canvas): 列出本会话工作区/agent「我的工作区」里的文件并预览。
 * 在线产物在服务端, 只能预览; 离线产物在本机工作区, 可定位/用系统程序打开。
 */
const emit = defineEmits<{ (e: 'close'): void }>()

interface ArtifactItem {
  relPath: string
  name: string
  size: number
  modified: string
}

interface ArtifactContent {
  kind: 'text' | 'image'
  name: string
  text?: string
  dataUrl?: string
}

const chat = useChatStore()
const items = ref<ArtifactItem[]>([])
const loading = ref(false)
const error = ref('')
const active = ref<ArtifactItem | null>(null)
const content = ref<ArtifactContent | null>(null)
const previewLoading = ref(false)

const isLocal = computed(() => chat.sessionMode === 'local')
const mode = computed<'local' | 'agent'>(() => (isLocal.value ? 'local' : 'agent'))

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function fmtTime(t: string): string {
  const d = new Date(t)
  return Number.isNaN(d.getTime()) ? t : d.toLocaleString('zh-CN', { hour12: false })
}

function ext(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot > 0 ? name.slice(dot).toLowerCase() : ''
}

async function loadList(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    items.value = await window.desktop.invoke<ArtifactItem[]>('artifact:list', {
      mode: mode.value,
      sessionId: chat.sessionId
    })
  } catch (err) {
    items.value = []
    error.value = (err as Error).message
  } finally {
    loading.value = false
  }
}

async function openItem(item: ArtifactItem): Promise<void> {
  active.value = item
  content.value = null
  previewLoading.value = true
  try {
    content.value = await window.desktop.invoke<ArtifactContent>('artifact:read', {
      mode: mode.value,
      sessionId: chat.sessionId,
      relPath: item.relPath
    })
  } catch (err) {
    ElMessage.warning((err as Error).message)
  } finally {
    previewLoading.value = false
  }
}

/** 本地产物: 在文件管理器中定位 / 用系统程序打开 */
async function reveal(item: ArtifactItem): Promise<void> {
  try {
    await window.desktop.invoke('artifact:reveal', { sessionId: chat.sessionId, relPath: item.relPath })
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function openInSystem(item: ArtifactItem): Promise<void> {
  try {
    const msg = await window.desktop.invoke<string>('artifact:open', {
      sessionId: chat.sessionId,
      relPath: item.relPath
    })
    if (msg) ElMessage.warning(msg)
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

/** CSV/TSV 简易表格(限 200 行 × 20 列) */
function csvRows(text: string, sep: string): string[][] {
  return text
    .split(/\r?\n/)
    .filter((line) => line.trim())
    .slice(0, 200)
    .map((line) => line.split(sep).slice(0, 20))
}

// 侧边栏挂载即加载
void loadList()

watch(
  () => [chat.sessionId, chat.sessionMode],
  () => {
    items.value = []
    active.value = null
    content.value = null
    void loadList()
  }
)
</script>

<template>
  <aside class="artifact-panel">
    <div class="artifact-inner">
      <header class="artifact-head">
        <span class="artifact-title">产物</span>
        <span class="artifact-hint">
          {{ isLocal ? '本地会话工作区' : '我的工作区(服务端)' }} · 共 {{ items.length }} 个文件
        </span>
        <el-button :icon="Refresh" size="small" :loading="loading" @click="loadList">刷新</el-button>
        <el-button :icon="Close" size="small" circle title="收起产物" @click="emit('close')" />
      </header>

      <div class="artifact-body">
        <aside class="artifact-list">
          <div v-if="error" class="artifact-empty">{{ error }}</div>
          <div v-else-if="items.length === 0 && !loading" class="artifact-empty">
            还没有产物文件；让 AI 生成报告/表格/网页后回到这里查看。
          </div>
          <button
            v-for="item in items"
            :key="item.relPath"
            type="button"
            class="artifact-item"
            :class="{ active: item.relPath === active?.relPath }"
            @click="openItem(item)"
          >
            <el-icon :size="13"><Document /></el-icon>
            <span class="artifact-name" :title="item.relPath">{{ item.name }}</span>
            <span class="artifact-meta">{{ fmtSize(item.size) }} · {{ fmtTime(item.modified) }}</span>
          </button>
        </aside>

        <section v-loading="previewLoading" class="artifact-preview">
          <div v-if="!active" class="artifact-empty">从左侧选择一个文件进行预览</div>
          <template v-else>
            <div class="artifact-preview-head">
              <span class="artifact-preview-name" :title="active.relPath">{{ active.relPath }}</span>
              <template v-if="isLocal">
                <el-button size="small" :icon="FolderOpened" @click="reveal(active)">定位</el-button>
                <el-button size="small" @click="openInSystem(active)">用系统程序打开</el-button>
              </template>
            </div>

            <div v-if="content" class="artifact-preview-body">
              <img v-if="content.kind === 'image'" class="artifact-image" :src="content.dataUrl" :alt="content.name" />
              <MarkdownBody
                v-else-if="['.md', '.markdown'].includes(ext(content.name))"
                class="artifact-md"
                :content="content.text ?? ''"
              />
              <table v-else-if="['.csv', '.tsv'].includes(ext(content.name))" class="artifact-table">
                <tbody>
                  <tr v-for="(row, i) in csvRows(content.text ?? '', ext(content.name) === '.tsv' ? '\t' : ',')" :key="i">
                    <td v-for="(cell, j) in row" :key="j">{{ cell }}</td>
                  </tr>
                </tbody>
              </table>
              <iframe
                v-else-if="['.html', '.htm'].includes(ext(content.name))"
                class="artifact-frame"
                sandbox="allow-scripts"
                :srcdoc="content.text ?? ''"
              />
              <pre v-else class="artifact-text">{{ content.text }}</pre>
            </div>
          </template>
        </section>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.artifact-panel {
  flex: none;
  width: 46%;
  min-width: 360px;
  max-width: 720px;
  display: flex;
  flex-direction: column;
  height: 100%;
  border-left: 1px solid var(--el-border-color-lighter);
  background: var(--el-bg-color);
  padding: 12px 14px;
  overflow: hidden;
}

.artifact-inner { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.artifact-head { display: flex; align-items: center; gap: 10px; padding-bottom: 10px; border-bottom: 1px solid var(--el-border-color-lighter); }
.artifact-title { font-size: 15px; font-weight: 600; }
.artifact-hint { flex: 1; font-size: 12px; color: var(--el-text-color-secondary); }
.artifact-body { flex: 1; min-height: 0; display: flex; gap: 12px; padding-top: 10px; }
.artifact-list { width: 260px; flex: none; overflow-y: auto; border-right: 1px solid var(--el-border-color-lighter); padding-right: 8px; }
.artifact-item {
  display: flex; align-items: center; gap: 6px; width: 100%; text-align: left;
  background: transparent; border: none; border-radius: 8px; padding: 6px 8px; cursor: pointer;
  color: var(--el-text-color-regular); font-size: 12.5px;
}
.artifact-item:hover { background: var(--el-fill-color-light); }
.artifact-item.active { background: var(--el-fill-color-darker); color: var(--el-text-color-primary); }
.artifact-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.artifact-meta { flex: none; font-size: 11px; color: var(--el-text-color-secondary); }
.artifact-preview { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.artifact-preview-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.artifact-preview-name { flex: 1; font-size: 12.5px; color: var(--el-text-color-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.artifact-preview-body { flex: 1; min-height: 0; overflow: auto; }
.artifact-image { max-width: 100%; }
.artifact-text { margin: 0; padding: 10px 12px; background: var(--el-fill-color-light); border-radius: 8px; font-family: Consolas, monospace; font-size: 12.5px; white-space: pre-wrap; }
.artifact-table { border-collapse: collapse; font-size: 12px; }
.artifact-table td { border: 1px solid var(--el-border-color-lighter); padding: 3px 8px; max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.artifact-frame { width: 100%; height: 100%; min-height: 420px; border: 1px solid var(--el-border-color-lighter); border-radius: 8px; background: #fff; }
.artifact-empty { padding: 18px 10px; font-size: 12.5px; color: var(--el-text-color-secondary); }
</style>
