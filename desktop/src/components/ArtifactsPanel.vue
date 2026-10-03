<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Close, Delete, Document, Folder, FolderOpened, Refresh } from '@element-plus/icons-vue'
import { useChatStore } from '../stores/chat'
import MarkdownBody from './MarkdownBody.vue'

/**
 * 对话产物侧边栏(Canvas): 以树状结构列出本会话工作区/agent「我的工作区」里的文件并预览。
 * 在线产物在服务端, 可预览/删除; 离线产物在本机工作区, 可定位/用系统程序打开/删除。
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

interface TreeNode {
  key: string
  label: string
  isDir: boolean
  relPath?: string
  size?: number
  modified?: string
  children?: TreeNode[]
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

/** 树状结构: 由扁平 relPath 聚合出目录层级 */
const treeData = computed<TreeNode[]>(() => {
  const root: TreeNode[] = []
  const dirs = new Map<string, TreeNode>()
  function childrenOf(parts: string[]): TreeNode[] {
    let children = root
    let path = ''
    for (const part of parts) {
      path = path ? `${path}/${part}` : part
      let node = dirs.get(path)
      if (!node) {
        node = { key: `dir:${path}`, label: part, isDir: true, children: [] }
        dirs.set(path, node)
        children.push(node)
      }
      children = node.children!
    }
    return children
  }
  for (const it of items.value) {
    const parts = it.relPath.split('/')
    const name = parts.pop() ?? it.relPath
    childrenOf(parts).push({
      key: `file:${it.relPath}`,
      label: name,
      isDir: false,
      relPath: it.relPath,
      size: it.size,
      modified: it.modified
    })
  }
  const sortNodes = (nodes: TreeNode[]): void => {
    nodes.sort((a, b) => {
      if (a.isDir !== b.isDir) return a.isDir ? -1 : 1
      return a.label.localeCompare(b.label, 'zh-CN')
    })
    for (const n of nodes) if (n.children) sortNodes(n.children)
  }
  sortNodes(root)
  return root
})

/** 面板宽度(可拖拽调整) */
const panelWidth = ref(760)
const MIN_WIDTH = 380
let resizing = false
let startX = 0
let startW = 0

function onResizeMove(e: MouseEvent): void {
  if (!resizing) return
  const max = Math.max(MIN_WIDTH, window.innerWidth - 360)
  panelWidth.value = Math.min(Math.max(startW - (e.clientX - startX), MIN_WIDTH), max)
}
function stopResize(): void {
  resizing = false
  window.removeEventListener('mousemove', onResizeMove)
  window.removeEventListener('mouseup', stopResize)
  document.body.style.userSelect = ''
}
function startResize(e: MouseEvent): void {
  resizing = true
  startX = e.clientX
  startW = panelWidth.value
  window.addEventListener('mousemove', onResizeMove)
  window.addEventListener('mouseup', stopResize)
  document.body.style.userSelect = 'none'
  e.preventDefault()
}

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

function onNodeClick(data: TreeNode): void {
  if (data.isDir || !data.relPath) return
  void openItem({
    relPath: data.relPath,
    name: data.label,
    size: data.size ?? 0,
    modified: data.modified ?? ''
  })
}

/** 删除产物(在线/离线) */
async function removeItem(item: ArtifactItem): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除「${item.relPath}」吗？此操作不可恢复。`, '删除产物', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消'
    })
  } catch {
    return
  }
  try {
    await window.desktop.invoke('artifact:delete', {
      mode: mode.value,
      sessionId: chat.sessionId,
      relPath: item.relPath
    })
    ElMessage.success('已删除')
    if (active.value?.relPath === item.relPath) {
      active.value = null
      content.value = null
    }
    await loadList()
  } catch (err) {
    ElMessage.error((err as Error).message)
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

onBeforeUnmount(stopResize)
</script>

<template>
  <aside class="artifact-panel" :style="{ width: `${panelWidth}px` }">
    <div class="artifact-resizer" title="拖动调整宽度" @mousedown="startResize" />
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
          <el-tree
            v-else
            class="artifact-tree"
            :data="treeData"
            node-key="key"
            :props="{ label: 'label', children: 'children' }"
            default-expand-all
            highlight-current
            @node-click="onNodeClick"
          >
            <template #default="{ data }">
              <div class="artifact-node" :class="{ active: !data.isDir && data.relPath === active?.relPath }">
                <el-icon :size="14" class="artifact-node-icon">
                  <Folder v-if="data.isDir" />
                  <Document v-else />
                </el-icon>
                <span class="artifact-node-name" :title="data.relPath || data.label">{{ data.label }}</span>
                <span v-if="!data.isDir" class="artifact-node-meta">{{ fmtSize(data.size) }}</span>
                <el-icon
                  v-if="!data.isDir"
                  class="artifact-del"
                  title="删除"
                  @click.stop="removeItem({ relPath: data.relPath, name: data.label, size: data.size, modified: data.modified })"
                >
                  <Delete />
                </el-icon>
              </div>
            </template>
          </el-tree>
        </aside>

        <section v-loading="previewLoading" class="artifact-preview">
          <div v-if="!active" class="artifact-empty">从左侧选择一个文件进行预览</div>
          <template v-else>
            <div class="artifact-preview-head">
              <span class="artifact-preview-name" :title="active.relPath">{{ active.relPath }}</span>
              <span class="artifact-preview-sub">{{ fmtSize(active.size) }} · {{ fmtTime(active.modified) }}</span>
              <template v-if="isLocal">
                <el-button size="small" :icon="FolderOpened" @click="reveal(active)">定位</el-button>
                <el-button size="small" @click="openInSystem(active)">用系统程序打开</el-button>
              </template>
              <el-button size="small" type="danger" plain :icon="Delete" @click="removeItem(active)">删除</el-button>
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
  position: relative;
  flex: none;
  width: 760px;
  min-width: 380px;
  max-width: 92%;
  display: flex;
  flex-direction: column;
  height: 100%;
  border-left: 1px solid var(--el-border-color-lighter);
  background: var(--el-bg-color);
  padding: 12px 14px;
  overflow: hidden;
}

/* 左边缘拖拽把手: 调整面板宽度 */
.artifact-resizer {
  position: absolute;
  left: 0;
  top: 0;
  width: 6px;
  height: 100%;
  cursor: col-resize;
  z-index: 2;
}
.artifact-resizer:hover { background: var(--el-color-primary-light-7); }

.artifact-inner { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.artifact-head { display: flex; align-items: center; gap: 10px; padding-bottom: 10px; border-bottom: 1px solid var(--el-border-color-lighter); }
.artifact-title { font-size: 15px; font-weight: 600; }
.artifact-hint { flex: 1; font-size: 12px; color: var(--el-text-color-secondary); }
.artifact-body { flex: 1; min-height: 0; display: flex; gap: 12px; padding-top: 10px; }
.artifact-list { width: 280px; flex: none; overflow: auto; border-right: 1px solid var(--el-border-color-lighter); padding-right: 8px; }

.artifact-tree { background: transparent; --el-tree-node-hover-bg-color: var(--el-fill-color-light); }
.artifact-tree :deep(.el-tree-node__content) { height: 30px; border-radius: 6px; }
.artifact-node {
  display: flex; align-items: center; gap: 6px; flex: 1; min-width: 0;
  padding-right: 4px; font-size: 12.5px; color: var(--el-text-color-regular);
}
.artifact-node.active { color: var(--el-color-primary); }
.artifact-node-icon { flex: none; color: var(--el-color-warning); }
.artifact-node-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.artifact-node-meta { flex: none; font-size: 11px; color: var(--el-text-color-secondary); }
.artifact-del {
  flex: none; font-size: 13px; color: var(--el-text-color-secondary);
  opacity: 0; cursor: pointer; transition: opacity 0.12s ease, color 0.12s ease;
}
.artifact-node:hover .artifact-del { opacity: 1; }
.artifact-del:hover { color: var(--el-color-danger); }

.artifact-preview { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.artifact-preview-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.artifact-preview-name { flex: 1; font-size: 12.5px; color: var(--el-text-color-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.artifact-preview-sub { flex: none; font-size: 11.5px; color: var(--el-text-color-secondary); }
.artifact-preview-body { flex: 1; min-height: 0; overflow: auto; }
.artifact-image { max-width: 100%; }
.artifact-text { margin: 0; padding: 10px 12px; background: var(--el-fill-color-light); border-radius: 8px; font-family: Consolas, monospace; font-size: 12.5px; white-space: pre-wrap; }
.artifact-table { border-collapse: collapse; font-size: 12px; }
.artifact-table td { border: 1px solid var(--el-border-color-lighter); padding: 3px 8px; max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.artifact-frame { width: 100%; height: 100%; min-height: 520px; border: 1px solid var(--el-border-color-lighter); border-radius: 8px; background: #fff; }
.artifact-empty { padding: 18px 10px; font-size: 12.5px; color: var(--el-text-color-secondary); }
</style>
