<script setup lang="ts">
defineOptions({ name: 'KnowledgeView' })
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Back, CaretRight, Document, Refresh, Search } from '@element-plus/icons-vue'
import MarkdownIt from 'markdown-it'
import { api, hasPerm } from '../api'

/** 只读知识库(经 agent /api/knowledge/* 代理到 RAG)。 */
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
const router = useRouter()

interface WikiPageMeta { id: string; title: string; summary?: string; updated_at: string; parent_id?: string | null }
interface WikiCategory { name: string; pages: WikiPageMeta[] }
interface WikiTreeItem {
  id: string; title: string; summary?: string; category?: string
  parent_id: string | null; position: number; space_id: string | null
}
interface WikiIndex { total: number; items?: WikiTreeItem[]; categories?: WikiCategory[]; running: boolean }
interface WikiSource { id: string; title: string }
interface WikiPage {
  id: string; title: string; category: string; content: string; summary: string
  sources: WikiSource[]; updated_at: string
}
interface WikiSpaceItem { id: string; name: string; icon: string; description: string; count: number }
interface WikiSpacesResp { spaces: WikiSpaceItem[]; default_count: number; total: number }
interface SearchHit { id: string; title: string; content: string; score: number; source: string; chunks?: unknown[] }

/** 两级视图: activeSpace=null 显示空间卡片列表; 否则进入该空间 wiki(id ''=全部, 'default'=默认空间) */
const activeSpace = ref<{ id: string; name: string; icon: string } | null>(null)
/** 「全部」(跨空间聚合)仅管理员可见, 普通用户按空间浏览 */
const isAdminUser = computed(() => hasPerm('*'))
const spaces = ref<WikiSpaceItem[]>([])
const spacesTotal = ref(0)
const spacesDefaultCount = ref(0)
const spacesLoading = ref(false)
const spacesError = ref('')
const spacesLoaded = ref(false)

const indexLoading = ref(false)
const indexError = ref('')
const indexLoaded = ref(false)
const index = ref<WikiIndex>({ total: 0, categories: [], running: false })
/** 树展开集合: 首次默认展开全部有子节点的节点, 刷新时保留仍存在的 id */
const expanded = ref<Set<string>>(new Set())
let treeInit = false
/** 当前选中页(树行只需最小字段; WikiTreeItem 与 WikiPageMeta 均可传入) */
const current = ref<{ id: string; title: string; summary?: string } | null>(null)
const page = ref<WikiPage | null>(null)
const pageLoading = ref(false)
const pageError = ref('')
let reqSeq = 0

const searchOpen = ref(false)
const searchInput = ref('')
const searching = ref(false)
const searchError = ref('')
const searchResults = ref<SearchHit[]>([])
const searchMeta = ref<{ total: number; graph_expanded?: boolean } | null>(null)
const expandedHits = ref<Set<string>>(new Set())

/** 无 SSO 会话时后端 fail-closed 503(引导登录), 与「未配置」区分 */
const needLogin = ref(false)

function errText(e: unknown): string {
  const x = e as { status?: number; message?: string }
  if (x?.status === 401) return '登录已过期，请重新登录'
  if (x?.status === 503) {
    if (x.message && x.message.includes('请先登录')) {
      needLogin.value = true
      return x.message
    }
    return 'RAG 知识库未配置 (RAG_BASE_URL)'
  }
  if (x?.status) return `知识库请求失败(HTTP ${x.status}):${x.message ?? ''}`
  return `RAG 知识库不可达:${(e as Error).message}`
}

async function loadSpaces(): Promise<void> {
  spacesLoading.value = true
  spacesError.value = ''
  needLogin.value = false
  try {
    const data = await api<WikiSpacesResp>('/api/knowledge/spaces')
    spaces.value = data.spaces ?? []
    spacesTotal.value = data.total ?? 0
    spacesDefaultCount.value = data.default_count ?? 0
    spacesLoaded.value = true
  } catch (e) {
    spacesError.value = errText(e)
  } finally {
    spacesLoading.value = false
  }
}

/** 进入空间(卡片点击): 重置阅读状态并按空间过滤目录 */
function enterSpace(id: string, name: string, icon: string): void {
  activeSpace.value = { id, name, icon }
  current.value = null
  page.value = null
  pageError.value = ''
  expanded.value = new Set()
  treeInit = false
  void loadIndex()
}

/** 返回空间卡片列表(刷新计数) */
function backToSpaces(): void {
  activeSpace.value = null
  current.value = null
  page.value = null
  pageError.value = ''
  void loadSpaces()
}

function refresh(): void {
  if (activeSpace.value) void loadIndex()
  else void loadSpaces()
}

async function loadIndex(): Promise<void> {
  indexLoading.value = true
  indexError.value = ''
  needLogin.value = false
  try {
    const sid = activeSpace.value?.id ?? ''
    const qs = sid ? '?space_id=' + encodeURIComponent(sid) : ''
    const data = await api<WikiIndex>('/api/knowledge/wiki' + qs)
    index.value = data
    indexLoaded.value = true
    syncTreeExpanded(data)
  } catch (e) {
    indexError.value = errText(e)
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

async function openPage(meta: { id: string; title: string; summary?: string }): Promise<void> {
  current.value = meta
  const seq = ++reqSeq
  page.value = null
  pageError.value = ''
  needLogin.value = false
  pageLoading.value = true
  try {
    const data = await api<WikiPage>('/api/knowledge/wiki/' + encodeURIComponent(meta.id))
    if (seq !== reqSeq) return
    page.value = data
  } catch (e) {
    if (seq !== reqSeq) return
    pageError.value = errText(e)
  } finally {
    if (seq === reqSeq) pageLoading.value = false
  }
}

async function runSearch(): Promise<void> {
  const q = searchInput.value.trim()
  if (!q || searching.value) return
  searching.value = true
  searchError.value = ''
  needLogin.value = false
  try {
    const data = await api<{ results: SearchHit[]; total: number; graph_expanded?: boolean }>(
      '/api/knowledge/search?q=' + encodeURIComponent(q) + '&top_k=8'
    )
    searchResults.value = data.results ?? []
    searchMeta.value = { total: data.total ?? 0, graph_expanded: data.graph_expanded }
    expandedHits.value = new Set()
  } catch (e) {
    searchResults.value = []
    searchMeta.value = null
    searchError.value = errText(e)
  } finally {
    searching.value = false
  }
}

function toggleHit(id: string): void {
  const next = new Set(expandedHits.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expandedHits.value = next
}

const renderedContent = computed(() => (page.value?.content ? md.render(page.value.content) : ''))

function fmtTime(t: string): string {
  const d = new Date(t)
  if (Number.isNaN(d.getTime())) return String(t)
  return d.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

onMounted(() => void loadSpaces())
</script>

<template>
  <div class="kb-view">
    <div class="view-header">
      <div class="view-header-left">
        <el-button v-if="activeSpace" :icon="Back" size="small" circle text title="返回空间列表" @click="backToSpaces" />
        <span class="view-title">{{ activeSpace ? activeSpace.icon + ' ' + activeSpace.name : '知识库' }}</span>
        <span v-if="!activeSpace && spacesLoaded && spacesTotal > 0" class="kb-count">共 {{ spacesTotal }} 篇</span>
        <span v-if="activeSpace && indexLoaded && index.total > 0" class="kb-count">共 {{ index.total }} 篇</span>
        <el-tag v-if="activeSpace && index.running" size="small" type="warning" effect="plain">知识库整理中</el-tag>
      </div>
      <div class="kb-head-actions">
        <el-button :icon="Search" size="small" circle title="检索知识库" @click="searchOpen = true" />
        <el-button :icon="Refresh" size="small" circle title="刷新" :loading="indexLoading || spacesLoading" @click="refresh" />
      </div>
    </div>

    <el-dialog v-model="searchOpen" title="检索知识库" width="720px" top="8vh" align-center>
      <div class="kb-search-bar">
        <el-input v-model="searchInput" placeholder="输入关键词，Enter 检索(混合检索 + 重排 + 图谱扩展)" clearable @keyup.enter="runSearch">
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-button type="primary" :loading="searching" @click="runSearch">检索</el-button>
      </div>
      <div v-loading="searching" class="kb-search-body">
        <div v-if="searchError" class="kb-state"><el-empty :description="searchError"><el-button v-if="needLogin" type="primary" size="small" @click="router.push('/login')">前往登录</el-button><el-button size="small" @click="runSearch">重试</el-button></el-empty></div>
        <template v-else-if="searchResults.length || searchMeta">
          <div class="kb-search-meta">
            共 {{ searchMeta?.total ?? 0 }} 条命中<template v-if="searchMeta?.graph_expanded">，已启用图谱扩展</template>
          </div>
          <div v-if="searchResults.length === 0 && !searching" class="kb-state"><el-empty description="没有找到相关内容，换个说法再试" /></div>
          <div v-for="hit in searchResults" :key="hit.id" class="kb-hit" @click="toggleHit(hit.id)">
            <div class="kb-hit-head">
              <span class="kb-hit-title">{{ hit.title }}</span>
              <el-tag size="small" type="info" effect="plain">{{ hit.source }}</el-tag>
              <span class="kb-hit-score">{{ hit.score.toFixed(3) }}</span>
            </div>
            <p class="kb-hit-body">{{ hit.content }}</p>
            <div v-if="expandedHits.has(hit.id) && (hit.chunks ?? []).length" class="kb-hit-chunks">
              <pre v-for="(chunk, i) in hit.chunks ?? []" :key="i" class="kb-hit-chunk">{{ typeof chunk === 'string' ? chunk : JSON.stringify(chunk, null, 2) }}</pre>
            </div>
            <div v-else-if="(hit.chunks ?? []).length" class="kb-hit-more">点击展开 {{ (hit.chunks ?? []).length }} 个命中分片</div>
          </div>
        </template>
        <div v-else class="kb-state"><el-empty description="输入关键词开始检索，例如「报销制度」「设备故障排查」" /></div>
      </div>
    </el-dialog>

    <div v-loading="spacesLoading || indexLoading" class="kb-body">
      <!-- 第一级: 空间卡片列表 -->
      <div v-if="!activeSpace" class="kb-spaces">
        <div v-if="spacesError" class="kb-state">
          <el-empty :description="spacesError"><el-button v-if="needLogin" type="primary" size="small" @click="router.push('/login')">前往登录</el-button><el-button size="small" @click="loadSpaces">重试</el-button></el-empty>
        </div>
        <div v-else-if="spacesLoaded && spacesTotal === 0" class="kb-state"><el-empty description="知识库还没有内容" /></div>
        <div v-else-if="spacesLoaded" class="kb-spaces-grid">
          <button v-if="isAdminUser" class="kb-space-card" @click="enterSpace('', '全部', '📚')">
            <span class="kb-space-icon">📚</span>
            <span class="kb-space-main">
              <span class="kb-space-name">全部</span>
              <span class="kb-space-desc">全库 Wiki 页面</span>
            </span>
            <span class="kb-space-count">{{ spacesTotal }} 篇</span>
          </button>
          <button v-if="spacesDefaultCount > 0" class="kb-space-card" @click="enterSpace('default', '默认空间', '📄')">
            <span class="kb-space-icon">📄</span>
            <span class="kb-space-main">
              <span class="kb-space-name">默认空间</span>
              <span class="kb-space-desc">未归入其它空间的页面</span>
            </span>
            <span class="kb-space-count">{{ spacesDefaultCount }} 篇</span>
          </button>
          <button v-for="s in spaces" :key="s.id" class="kb-space-card" @click="enterSpace(s.id, s.name, s.icon || '🗂️')">
            <span class="kb-space-icon">{{ s.icon || '🗂️' }}</span>
            <span class="kb-space-main">
              <span class="kb-space-name">{{ s.name }}</span>
              <span v-if="s.description" class="kb-space-desc">{{ s.description }}</span>
            </span>
            <span class="kb-space-count">{{ s.count }} 篇</span>
          </button>
        </div>
        <div v-else class="kb-state"><el-empty description="加载中…" /></div>
      </div>

      <!-- 第二级: 空间内 Wiki -->
      <template v-else>
        <div v-if="indexError" class="kb-state">
          <el-empty :description="indexError"><el-button v-if="needLogin" type="primary" size="small" @click="router.push('/login')">前往登录</el-button><el-button size="small" @click="loadIndex">重试</el-button></el-empty>
        </div>
        <div v-else-if="indexLoaded && index.total === 0" class="kb-state"><el-empty description="该空间还没有内容" /></div>
        <div v-else-if="indexLoaded" class="kb-body-inner">
        <aside class="kb-tree">
          <div
            v-for="row in treeRows"
            :key="row.page.id"
            class="kb-tree-row"
            :class="{ active: row.page.id === current?.id }"
            :style="{ paddingLeft: (8 + row.depth * 14) + 'px' }"
            @click="openPage(row.page)"
          >
            <span
              v-if="row.hasChildren"
              class="kb-tree-caret"
              :class="{ open: expanded.has(row.page.id) }"
              @click.stop="toggleNode(row.page.id)"
            ><el-icon :size="13"><CaretRight /></el-icon></span>
            <span v-else class="kb-tree-caret kb-tree-caret-placeholder"></span>
            <span class="kb-tree-title">{{ row.page.title }}</span>
          </div>
        </aside>

        <section class="kb-reader">
          <el-skeleton v-if="pageLoading" :rows="8" animated />
          <div v-else-if="pageError" class="kb-state">
            <el-empty :description="pageError"><el-button v-if="needLogin" type="primary" size="small" @click="router.push('/login')">前往登录</el-button><el-button size="small" @click="current && openPage(current)">重试</el-button></el-empty>
          </div>
          <div v-else-if="page" class="kb-doc">
            <header class="kb-doc-head">
              <div class="kb-doc-title-row">
                <h2 class="kb-doc-title">{{ page.title }}</h2>
                <el-tag v-if="page.category" size="small" type="info" effect="plain">{{ page.category }}</el-tag>
              </div>
              <span class="kb-doc-time">更新于 {{ fmtTime(page.updated_at) }}</span>
            </header>
            <p v-if="page.summary" class="kb-doc-summary">{{ page.summary }}</p>
            <div v-if="page.content" class="md kb-content" v-html="renderedContent" />
            <div v-if="(page.sources ?? []).length" class="kb-sources">
              <div class="kb-sources-title">来源笔记({{ (page.sources ?? []).length }})</div>
              <div v-for="s in page.sources ?? []" :key="s.id" class="kb-source">
                <el-icon :size="14" class="kb-source-icon"><Document /></el-icon>
                <div class="kb-source-main">
                  <span class="kb-source-title">{{ s.title }}</span>
                  <span class="kb-source-id">{{ s.id }}</span>
                </div>
              </div>
            </div>
          </div>
          <div v-else class="kb-state"><el-empty description="从左侧选择一篇文章开始阅读" /></div>
        </section>
      </div>
        <div v-else class="kb-state"><el-empty description="加载中…" /></div>
      </template>
    </div>
  </div>
</template>
