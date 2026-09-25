<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { CaretRight, Document, Refresh, Search } from '@element-plus/icons-vue'
import { ApiError, request } from '../api/client'
import type { WikiIndex, WikiPage, WikiPageMeta } from '../api/types'
import { useSettingsStore } from '../stores/settings'
import MarkdownBody from '../components/MarkdownBody.vue'

const router = useRouter()
const settings = useSettingsStore()

const indexLoading = ref(false)
const indexError = ref('')
const indexLoaded = ref(false)
const index = ref<WikiIndex>({ total: 0, categories: [], running: false })
const openCats = ref<string[]>([])
/** 当前选中页的目录元信息(选中高亮 / 加载中标题 / 失败重试) */
const current = ref<WikiPageMeta | null>(null)
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

async function loadIndex() {
  if (!settings.hasUser) return
  indexLoading.value = true
  indexError.value = ''
  try {
    const data = await request<WikiIndex>('rag', '/api/wiki')
    index.value = data
    indexLoaded.value = true
    // 保留仍存在的已展开分类;确保当前阅读页所在分类可见,避免高亮与内容脱节
    const prevOpen = openCats.value
    const names = (data.categories ?? []).map((c) => c.name)
    let next = prevOpen.filter((n) => names.includes(n))
    if (current.value) {
      const holder = (data.categories ?? []).find((c) => c.pages.some((p) => p.id === current.value?.id))
      if (holder && !next.includes(holder.name)) next = [...next, holder.name]
    }
    if (next.length === 0 && data.categories.length) next = [data.categories[0].name]
    openCats.value = next
  } catch (err) {
    indexError.value = ragError(err)
  } finally {
    indexLoading.value = false
  }
}

async function openPage(meta: WikiPageMeta) {
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

function toggleCat(name: string) {
  openCats.value = openCats.value.includes(name)
    ? openCats.value.filter((n) => n !== name)
    : [...openCats.value, name]
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
  if (settings.hasUser) void loadIndex()
})
</script>

<template>
  <div class="knowledge-view">
    <header class="view-header">
      <div class="view-header-left">
        <span class="view-title">知识库</span>
        <span v-if="indexLoaded && index.total > 0" class="knowledge-count">共 {{ index.total }} 篇</span>
        <el-tag v-if="index.running" size="small" type="warning" effect="plain" class="knowledge-running">
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
          title="刷新目录"
          :loading="indexLoading"
          :disabled="!settings.hasUser"
          @click="loadIndex"
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

    <div v-loading="indexLoading" class="knowledge-body">
      <!-- 未登录:先完成企业 SSO 登录 -->
      <div v-if="!settings.hasUser" class="knowledge-state">
        <el-empty description="登录后可查看知识库">
          <el-button type="primary" size="small" @click="router.push('/settings')">前往登录</el-button>
        </el-empty>
      </div>

      <!-- 目录加载失败 -->
      <div v-else-if="indexError" class="knowledge-state">
        <el-empty :description="indexError">
          <el-button size="small" @click="loadIndex">重试</el-button>
        </el-empty>
      </div>

      <!-- 知识库为空 -->
      <div v-else-if="indexLoaded && index.total === 0" class="knowledge-state">
        <el-empty description="知识库还没有内容" />
      </div>

      <div v-else-if="indexLoaded" class="knowledge-body-inner">
        <aside class="knowledge-cats">
          <div v-for="cat in index.categories" :key="cat.name" class="knowledge-cat">
            <button class="knowledge-cat-head" :class="{ open: openCats.includes(cat.name) }" @click="toggleCat(cat.name)">
              <el-icon :size="13" class="knowledge-caret"><CaretRight /></el-icon>
              <span class="knowledge-cat-name">{{ cat.name }}</span>
              <span class="knowledge-cat-count">{{ cat.pages.length }}</span>
            </button>
            <div v-show="openCats.includes(cat.name)" class="knowledge-cat-list">
              <button
                v-for="p in cat.pages"
                :key="p.id"
                class="knowledge-page"
                :class="{ active: p.id === current?.id }"
                @click="openPage(p)"
              >
                <span class="knowledge-page-title">{{ p.title }}</span>
                <span v-if="p.summary" class="knowledge-page-sum">{{ p.summary }}</span>
              </button>
            </div>
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
    </div>
  </div>
</template>
