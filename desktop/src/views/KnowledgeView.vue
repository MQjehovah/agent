<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Back, CaretRight, Document, Refresh, Search } from '@element-plus/icons-vue'
import { ApiError, request } from '../api/client'
import type { WikiIndex, WikiPage, WikiSpaceItem, WikiSpacesResp, WikiTreeItem } from '../api/types'
import { useSettingsStore } from '../stores/settings'
import MarkdownBody from '../components/MarkdownBody.vue'

const router = useRouter()
const settings = useSettingsStore()

const indexLoading = ref(false)
const indexError = ref('')
const indexLoaded = ref(false)
const index = ref<WikiIndex>({ total: 0, categories: [], running: false })

/** 两级视图: activeSpace=null 显示空间卡片列表; 否则进入该空间 wiki(id ''=全部, 'default'=默认空间) */
const activeSpace = ref<{ id: string; name: string; icon: string } | null>(null)
/** 「全部」(跨空间聚合)仅管理员可见, 普通用户按空间浏览 */
const isAdminUser = () => (settings.user?.role || '') === 'admin'
const spaces = ref<WikiSpaceItem[]>([])
const spacesTotal = ref(0)
const spacesDefaultCount = ref(0)
const spacesLoading = ref(false)
const spacesError = ref('')
const spacesLoaded = ref(false)
/** 树展开集合: 首次默认展开全部有子节点的节点, 刷新时保留仍存在的 id */
const expanded = ref<Set<string>>(new Set())
let treeInit = false
/** 当前选中页(树行只需最小字段; WikiTreeItem 与 WikiPageMeta 均可传入) */
const current = ref<{ id: string; title: string; summary?: string } | null>(null)
const page = ref<WikiPage | null>(null)
const pageLoading = ref(false)
const pageError = ref('')
let reqSeq = 0

/** 知识库检索:调用 RAG 的 /api/search(混合检索 + 重排) */
interface SearchHit {
  id: string
  title: string
  content: string
  score: number
  source: string
  chunks?: unknown[]
}

const searchOpen = ref(false)
const searchRef = ref<{ focus: () => void } | null>(null)

function openSearch(): void {
  searchOpen.value = true
}

function focusSearchInput(): void {
  searchRef.value?.focus()
}

/**
 * 非模态弹窗: 手动实现「点击外部关闭」。
 * 注意 el-dialog 的 $el 是 teleport 占位节点(不含弹窗 DOM), 所以按弹窗 class 判断。
 */
function onDocMouseDown(e: MouseEvent): void {
  if (!searchOpen.value) return
  const target = e.target as HTMLElement | null
  // 弹窗本体或 Element 的对话框容器内都视为内部
  if (target?.closest?.('.kb-search-dialog, .el-dialog, .el-overlay-dialog')) return
  searchOpen.value = false
}

onMounted(() => {
  document.addEventListener('mousedown', onDocMouseDown, true)
})

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocMouseDown, true)
})

const searchQuery = ref('')
const searchInput = ref('')
const searching = ref(false)
const searchError = ref('')
const searchResults = ref<SearchHit[]>([])
const searchMeta = ref<{ total: number; graph_expanded?: boolean } | null>(null)
const expandedHits = ref<Set<string>>(new Set())

async function runSearch(): Promise<void> {
  const q = searchInput.value.trim()
  if (!q || searching.value) return
  searchQuery.value = q
  searching.value = true
  searchError.value = ''
  try {
    const data = await request<{ results: SearchHit[]; total: number; graph_expanded?: boolean }>(
      'rag',
      '/api/search',
      { method: 'POST', body: { query: q, top_k: 8 } }
    )
    searchResults.value = data.results ?? []
    searchMeta.value = { total: data.total ?? 0, graph_expanded: data.graph_expanded }
    expandedHits.value = new Set()
  } catch (err) {
    searchResults.value = []
    searchMeta.value = null
    searchError.value = ragError(err)
  } finally {
    searching.value = false
  }
}

function clearSearch(): void {
  searchQuery.value = ''
  searchInput.value = ''
  searchResults.value = []
  searchMeta.value = null
  searchError.value = ''
}

function toggleHit(id: string): void {
  const next = new Set(expandedHits.value)
  next.has(id) ? next.delete(id) : next.add(id)
  expandedHits.value = next
}

function ragError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) {
      return '未获得 RAG 知识库访问授权(401),请先完成企业 SSO 登录,并在「设置」中确认 RAG 地址正确'
    }
    return `知识库请求失败(HTTP ${err.status}):${err.message}`
  }
  return `RAG 知识库不可达:${(err as Error).message}`
}

async function loadSpaces() {
  if (!settings.hasUser) return
  spacesLoading.value = true
  spacesError.value = ''
  try {
    const data = await request<WikiSpacesResp>('rag', '/api/wiki/spaces')
    spaces.value = data.spaces ?? []
    spacesTotal.value = data.total ?? 0
    spacesDefaultCount.value = data.default_count ?? 0
    spacesLoaded.value = true
  } catch (err) {
    spacesError.value = ragError(err)
  } finally {
    spacesLoading.value = false
  }
}

/** 进入空间(卡片点击): 重置阅读状态并按空间过滤目录 */
function enterSpace(id: string, name: string, icon: string) {
  activeSpace.value = { id, name, icon }
  current.value = null
  page.value = null
  pageError.value = ''
  expanded.value = new Set()
  treeInit = false
  void loadIndex()
}

/** 返回空间卡片列表(刷新计数) */
function backToSpaces() {
  activeSpace.value = null
  current.value = null
  page.value = null
  pageError.value = ''
  void loadSpaces()
}

function refresh() {
  if (activeSpace.value) void loadIndex()
  else void loadSpaces()
}

async function loadIndex() {
  if (!settings.hasUser) return
  indexLoading.value = true
  indexError.value = ''
  try {
    const sid = activeSpace.value?.id ?? ''
    const qs = sid ? '?space_id=' + encodeURIComponent(sid) : ''
    const data = await request<WikiIndex>('rag', '/api/wiki' + qs)
    index.value = data
    indexLoaded.value = true
    syncTreeExpanded(data)
  } catch (err) {
    indexError.value = ragError(err)
  } finally {
    indexLoading.value = false
  }
}

/** 目录树数据源: 优先 items(真树); items 缺失/为空时回退 categories 摊平为根级行(兼容老 RAG) */
function wikiItemsOf(data: WikiIndex): WikiTreeItem[] {
  const items = data.items ?? []
  if (items.length) return items
  return (data.categories ?? []).flatMap((c) =>
    (c.pages ?? []).map((p) => ({ ...p, parent_id: null, position: 0, space_id: null }))
  )
}

/** parent_id 为空或指向不可见节点(不在 byId)视为根; 子节点按 position 升序(同 position 按 title) */
const treeChildren = computed<Map<string | null, WikiTreeItem[]>>(() => {
  const src = wikiItemsOf(index.value)
  const byId = new Map(src.map((p) => [p.id, p] as const))
  const map = new Map<string | null, WikiTreeItem[]>()
  for (const p of src) {
    const pid = p.parent_id && p.parent_id !== p.id && byId.has(p.parent_id) ? p.parent_id : null
    const list = map.get(pid)
    if (list) list.push(p)
    else map.set(pid, [p])
  }
  for (const list of map.values()) {
    list.sort((a, b) => (a.position ?? 0) - (b.position ?? 0) || (a.title ?? '').localeCompare(b.title ?? ''))
  }
  return map
})

/** DFS 展开为带缩进深度的行(折叠的子树不出行) */
const treeRows = computed<Array<{ page: WikiTreeItem; depth: number; hasChildren: boolean }>>(() => {
  const children = treeChildren.value
  const rows: Array<{ page: WikiTreeItem; depth: number; hasChildren: boolean }> = []
  const walk = (pid: string | null, depth: number): void => {
    for (const p of children.get(pid) ?? []) {
      const hasChildren = (children.get(p.id) ?? []).length > 0
      rows.push({ page: p, depth, hasChildren })
      if (hasChildren && expanded.value.has(p.id)) walk(p.id, depth + 1)
    }
  }
  walk(null, 0)
  return rows
})

/** 刷新索引后同步展开集合: 首次展开全部有子节点; 之后保留仍存在的 id 并自动展开当前选中页的祖先 */
function syncTreeExpanded(data: WikiIndex): void {
  const src = wikiItemsOf(data)
  const byId = new Map(src.map((p) => [p.id, p] as const))
  const childIds = new Set<string>()
  for (const p of src) {
    if (p.parent_id && p.parent_id !== p.id && byId.has(p.parent_id)) childIds.add(p.parent_id)
  }
  const next = treeInit
    ? new Set([...expanded.value].filter((id) => byId.has(id)))
    : new Set(childIds)
  treeInit = true
  let cur = current.value ? byId.get(current.value.id) : undefined
  while (cur && cur.parent_id && byId.has(cur.parent_id)) {
    if (next.has(cur.parent_id)) break
    next.add(cur.parent_id)
    cur = byId.get(cur.parent_id)
  }
  expanded.value = next
}

function toggleNode(id: string): void {
  const next = new Set(expanded.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expanded.value = next
}

async function openPage(meta: { id: string; title: string; summary?: string }) {
  current.value = meta
  const seq = ++reqSeq
  page.value = null
  pageError.value = ''
  pageLoading.value = true
  try {
    const data = await request<WikiPage>('rag', '/api/wiki/' + encodeURIComponent(meta.id))
    if (seq !== reqSeq) return
    page.value = data
  } catch (err) {
    if (seq !== reqSeq) return
    pageError.value = ragError(err)
  } finally {
    if (seq === reqSeq) pageLoading.value = false
  }
}

function formatTime(t: string): string {
  const d = new Date(t)
  if (Number.isNaN(d.getTime())) return String(t)
  return d.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

onMounted(() => {
  if (settings.hasUser) void loadSpaces()
})
</script>

<template>
  <div class="knowledge-view">
    <header class="view-header">
      <div class="view-header-left">
        <el-button v-if="activeSpace" :icon="Back" size="small" circle text title="返回空间列表" @click="backToSpaces" />
        <span class="view-title">{{ activeSpace ? activeSpace.icon + ' ' + activeSpace.name : '知识库' }}</span>
        <span v-if="!activeSpace && spacesLoaded && spacesTotal > 0" class="knowledge-count">共 {{ spacesTotal }} 篇</span>
        <span v-if="activeSpace && indexLoaded && index.total > 0" class="knowledge-count">共 {{ index.total }} 篇</span>
        <el-tag v-if="activeSpace && index.running" size="small" type="warning" effect="plain" class="knowledge-running">
          知识库整理中
        </el-tag>
      </div>
      <div class="knowledge-head-actions">
        <el-button
          :icon="Search"
          size="small"
          circle
          title="检索知识库"
          :disabled="!settings.hasUser"
          @click="openSearch"
        />
        <el-button
          :icon="Refresh"
          size="small"
          circle
          title="刷新"
          :loading="indexLoading || spacesLoading"
          :disabled="!settings.hasUser"
          @click="refresh"
        />
      </div>
    </header>

    <!-- 知识库检索弹窗(不带蒙版: 原生窗口按钮在蒙版之上, 避免视觉割裂) -->
    <el-dialog
      v-model="searchOpen"
      class="kb-search-dialog"
      title="检索知识库"
      width="720px"
      top="8vh"
      :modal="false"
      align-center
      @opened="focusSearchInput"
    >
      <div class="kb-search-bar">
        <el-input
          ref="searchRef"
          v-model="searchInput"
          placeholder="输入关键词,Enter 检索(混合检索 + 重排 + 图谱扩展)"
          clearable
          @keyup.enter="runSearch"
          @clear="clearSearch"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
        <el-button type="primary" :loading="searching" @click="runSearch">检索</el-button>
      </div>

      <div v-loading="searching" class="kb-search-body">
        <div v-if="searchError" class="knowledge-state">
          <el-empty :description="searchError">
            <el-button size="small" @click="runSearch">重试</el-button>
          </el-empty>
        </div>
        <template v-else-if="searchQuery">
          <div class="knowledge-search-meta">
            关键词「{{ searchQuery }}」共 {{ searchMeta?.total ?? 0 }} 条命中<template v-if="searchMeta?.graph_expanded">,已启用图谱扩展</template>
          </div>
          <div v-if="searchResults.length === 0 && !searching" class="knowledge-state">
            <el-empty description="没有找到相关内容,换个说法再试" />
          </div>
          <div v-for="hit in searchResults" :key="hit.id" class="knowledge-hit" @click="toggleHit(hit.id)">
            <div class="knowledge-hit-head">
              <span class="knowledge-hit-title">{{ hit.title }}</span>
              <el-tag size="small" type="info" effect="plain">{{ hit.source }}</el-tag>
              <span class="knowledge-hit-score">{{ hit.score.toFixed(3) }}</span>
            </div>
            <p class="knowledge-hit-body">{{ hit.content }}</p>
            <div v-if="expandedHits.has(hit.id) && (hit.chunks ?? []).length" class="knowledge-hit-chunks">
              <pre
                v-for="(chunk, i) in hit.chunks ?? []"
                :key="i"
                class="knowledge-hit-chunk"
              >{{ typeof chunk === 'string' ? chunk : JSON.stringify(chunk, null, 2) }}</pre>
            </div>
            <div v-else-if="(hit.chunks ?? []).length" class="knowledge-hit-more">
              点击展开 {{ (hit.chunks ?? []).length }} 个命中分片
            </div>
          </div>
        </template>
        <div v-else class="knowledge-state">
          <el-empty description="输入关键词开始检索,例如「报销制度」「设备故障排查」" />
        </div>
      </div>
    </el-dialog>

    <div v-loading="spacesLoading || indexLoading" class="knowledge-body">
      <!-- 未登录:先完成企业 SSO 登录 -->
      <div v-if="!settings.hasUser" class="knowledge-state">
        <el-empty description="登录后可查看知识库">
          <el-button type="primary" size="small" @click="router.push('/settings')">前往登录</el-button>
        </el-empty>
      </div>

      <!-- 第一级: 空间卡片列表 -->
      <div v-else-if="!activeSpace" class="knowledge-spaces">
        <div v-if="spacesError" class="knowledge-state">
          <el-empty :description="spacesError">
            <el-button size="small" @click="loadSpaces">重试</el-button>
          </el-empty>
        </div>
        <div v-else-if="spacesLoaded && spacesTotal === 0" class="knowledge-state">
          <el-empty description="知识库还没有内容" />
        </div>
        <div v-else-if="spacesLoaded" class="knowledge-spaces-grid">
          <button v-if="isAdminUser()" class="knowledge-space-card" @click="enterSpace('', '全部', '📚')">
            <span class="knowledge-space-icon">📚</span>
            <span class="knowledge-space-main">
              <span class="knowledge-space-name">全部</span>
              <span class="knowledge-space-desc">全库 Wiki 页面</span>
            </span>
            <span class="knowledge-space-count">{{ spacesTotal }} 篇</span>
          </button>
          <button v-if="spacesDefaultCount > 0" class="knowledge-space-card" @click="enterSpace('default', '默认空间', '📄')">
            <span class="knowledge-space-icon">📄</span>
            <span class="knowledge-space-main">
              <span class="knowledge-space-name">默认空间</span>
              <span class="knowledge-space-desc">未归入其它空间的页面</span>
            </span>
            <span class="knowledge-space-count">{{ spacesDefaultCount }} 篇</span>
          </button>
          <button v-for="s in spaces" :key="s.id" class="knowledge-space-card" @click="enterSpace(s.id, s.name, s.icon || '🗂️')">
            <span class="knowledge-space-icon">{{ s.icon || '🗂️' }}</span>
            <span class="knowledge-space-main">
              <span class="knowledge-space-name">{{ s.name }}</span>
              <span v-if="s.description" class="knowledge-space-desc">{{ s.description }}</span>
            </span>
            <span class="knowledge-space-count">{{ s.count }} 篇</span>
          </button>
        </div>
        <div v-else class="knowledge-state"><el-empty description="加载中…" /></div>
      </div>

      <!-- 第二级: 空间内 Wiki -->
      <template v-else>
        <!-- 目录加载失败 -->
        <div v-if="indexError" class="knowledge-state">
          <el-empty :description="indexError">
            <el-button size="small" @click="loadIndex">重试</el-button>
          </el-empty>
        </div>

        <!-- 空间为空 -->
        <div v-else-if="indexLoaded && index.total === 0" class="knowledge-state">
          <el-empty description="该空间还没有内容" />
        </div>

        <div v-else-if="indexLoaded" class="knowledge-body-inner">
        <aside class="knowledge-tree">
          <div
            v-for="row in treeRows"
            :key="row.page.id"
            class="knowledge-tree-row"
            :class="{ active: row.page.id === current?.id }"
            :style="{ paddingLeft: (8 + row.depth * 14) + 'px' }"
            @click="openPage(row.page)"
          >
            <span
              v-if="row.hasChildren"
              class="knowledge-tree-caret"
              :class="{ open: expanded.has(row.page.id) }"
              @click.stop="toggleNode(row.page.id)"
            ><el-icon :size="13"><CaretRight /></el-icon></span>
            <span v-else class="knowledge-tree-caret knowledge-tree-caret-placeholder"></span>
            <span class="knowledge-tree-title">{{ row.page.title }}</span>
          </div>
        </aside>

        <section class="knowledge-reader">
          <!-- 首次进入 / 加载中:骨架 -->
          <el-skeleton v-if="pageLoading" :rows="8" animated class="knowledge-skeleton" />

          <!-- 单页加载失败 -->
          <div v-else-if="pageError" class="knowledge-state">
            <el-empty :description="pageError">
              <el-button size="small" @click="current && openPage(current)">重试</el-button>
            </el-empty>
          </div>

          <!-- 已选中页:标题 + 正文 + 来源(全部只读) -->
          <div v-else-if="page" class="knowledge-doc">
            <header class="knowledge-doc-head">
              <div class="knowledge-doc-title-row">
                <h2 class="knowledge-doc-title">{{ page.title }}</h2>
                <el-tag v-if="page.category" size="small" type="info" effect="plain">{{ page.category }}</el-tag>
              </div>
              <span class="knowledge-doc-time">更新于 {{ formatTime(page.updated_at) }}</span>
            </header>

            <p v-if="page.summary" class="knowledge-doc-summary">{{ page.summary }}</p>

            <MarkdownBody v-if="page.content" class="knowledge-content" :content="page.content" />

            <div v-if="(page.sources ?? []).length" class="knowledge-sources">
              <div class="knowledge-sources-title">来源笔记({{ (page.sources ?? []).length }})</div>
              <div class="knowledge-source" v-for="s in page.sources ?? []" :key="s.id">
                <el-icon :size="14" class="knowledge-source-icon"><Document /></el-icon>
                <div class="knowledge-source-main">
                  <span class="knowledge-source-title">{{ s.title }}</span>
                  <span class="knowledge-source-id">{{ s.id }}</span>
                </div>
              </div>
            </div>
          </div>

          <!-- 未选中 -->
          <div v-else class="knowledge-state">
            <el-empty description="从左侧选择一篇文章开始阅读" />
          </div>
        </section>
      </div>
      </template>
    </div>
  </div>
</template>
