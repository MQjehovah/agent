<script setup lang="ts">
import { computed, nextTick, ref, watch, type Component } from 'vue'
import {
  ChatDotRound,
  Clock,
  Coin,
  Collection,
  Document,
  FolderOpened,
  MagicStick,
  Search,
  Setting,
  SwitchButton,
  Timer,
  TrendCharts
} from '@element-plus/icons-vue'
import { request } from '../api/client'
import { agentApi } from '../api/agent'
import type { WikiIndex } from '../api/types'
import { useSessionsStore, type SessionListItem } from '../stores/sessions'

const props = defineProps<{ modelValue: boolean }>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'navigate', path: string): void
  (e: 'open-session', item: SessionListItem): void
  (e: 'new-task'): void
}>()

/** 面板中一个可执行条目 */
interface PaletteItem {
  key: string
  title: string
  subtitle?: string
  icon?: Component
  /** 本地过滤匹配文本,缺省用 title */
  search?: string
  run: () => void
}

/** 渲染用的分组(含过滤后命中总数) */
interface PaletteGroupView {
  name: string
  total: number
  items: PaletteItem[]
}

/** 每组最多展示条数(空查询时命令组不受限) */
const MAX_PER_GROUP = 8

const sessionsStore = useSessionsStore()

const query = ref('')
const inputRef = ref<{ focus: () => void } | null>(null)
const activeKey = ref('')

/** 静态命令组:导航与新建任务 */
const commandItems: PaletteItem[] = [
  { key: 'cmd:chat', title: '对话', icon: ChatDotRound, run: () => emit('navigate', '/chat') },
  { key: 'cmd:sessions', title: '会话历史', icon: Clock, run: () => emit('navigate', '/sessions') },
  { key: 'cmd:knowledge', title: '知识库', icon: Collection, run: () => emit('navigate', '/knowledge') },
  { key: 'cmd:market', title: '能力市场', icon: MagicStick, run: () => emit('navigate', '/market') },
  { key: 'cmd:scheduler', title: '定时任务', icon: Timer, run: () => emit('navigate', '/scheduler') },
  { key: 'cmd:memories', title: '记忆管理', icon: Coin, run: () => emit('navigate', '/memories') },
  { key: 'cmd:usage', title: '用量', icon: TrendCharts, run: () => emit('navigate', '/usage') },
  { key: 'cmd:settings', title: '设置', icon: Setting, run: () => emit('navigate', '/settings') },
  {
    key: 'cmd:new',
    title: '新建任务',
    icon: SwitchButton,
    search: '新建任务 new task',
    run: () => emit('new-task')
  }
]

/** 会话组:来自布局常驻刷新的会话列表,无需额外请求 */
const sessionItems = computed<PaletteItem[]>(() =>
  sessionsStore.sessions.map((s) => ({
    key: `session:${s.mode}:${s.id}`,
    title: s.title,
    subtitle: `${s.mode === 'local' ? '本地' : '远程'} · ${formatTime(s.updatedAt)}`,
    icon: s.mode === 'local' ? FolderOpened : ChatDotRound,
    search: `${s.title} ${s.id}`,
    run: () => emit('open-session', s)
  }))
)

// ---- 知识库 / 能力市场:首次打开面板时各拉一次并缓存,失败静默为空组 ----

const wikiItems = ref<PaletteItem[]>([])
const wikiLoaded = ref(false)
const wikiLoading = ref(false)

const marketItems = ref<PaletteItem[]>([])
const marketLoaded = ref(false)
const marketLoading = ref(false)

/** portal /api/capabilities 分页外壳(容错只读,多余字段忽略) */
interface MarketCapPage {
  items?: unknown[]
}

/** 首次打开时拉取知识库索引,把 categories[].pages[] 摊平为条目 */
async function loadWiki(): Promise<void> {
  wikiLoading.value = true
  try {
    const data = await request<WikiIndex>('rag', '/api/wiki')
    const items: PaletteItem[] = []
    for (const cat of data.categories ?? []) {
      for (const page of cat.pages ?? []) {
        items.push({
          key: `wiki:${page.id}`,
          title: page.title,
          subtitle: cat.name,
          icon: Document,
          search: `${page.title} ${page.summary ?? ''} ${page.id}`,
          run: () => emit('navigate', '/knowledge')
        })
      }
    }
    wikiItems.value = items
  } catch {
    // 静默降级:该组为空,不弹错误
    wikiItems.value = []
  } finally {
    wikiLoaded.value = true
    wikiLoading.value = false
  }
}

/** 首次打开时拉取市场能力列表(单页),摊平 items 为条目 */
async function loadMarket(): Promise<void> {
  marketLoading.value = true
  try {
    const data = await request<MarketCapPage>('market', '/api/capabilities?page_size=100')
    const raw = Array.isArray(data?.items) ? data.items : []
    const items: PaletteItem[] = []
    for (const value of raw) {
      const cap = mapMarketItem(value)
      if (!cap) continue
      items.push({
        key: `market:${cap.id}`,
        title: cap.name,
        subtitle: cap.description,
        icon: MagicStick,
        search: `${cap.name} ${cap.description ?? ''} ${cap.id}`,
        run: () => emit('navigate', '/market')
      })
    }
    marketItems.value = items
  } catch {
    marketItems.value = []
  } finally {
    marketLoaded.value = true
    marketLoading.value = false
  }
}

/** portal 单条能力容错映射:缺 name 视为无效 */
function mapMarketItem(value: unknown): { id: string; name: string; description?: string } | null {
  if (typeof value !== 'object' || value === null) return null
  const r = value as Record<string, unknown>
  const name = typeof r.name === 'string' ? r.name : ''
  if (!name) return null
  const id = typeof r.id === 'string' && r.id ? r.id : name
  const description = typeof r.description === 'string' && r.description ? r.description : undefined
  return { id, name, description }
}

// ---- 会话内容搜索(服务端全文检索, 输入防抖 300ms) ----
const contentItems = ref<PaletteItem[]>([])
let searchTimer: number | undefined
let searchSeq = 0

async function runContentSearch(keyword: string): Promise<void> {
  const seq = ++searchSeq
  try {
    const res = await agentApi.searchSessions(keyword, 20)
    if (seq !== searchSeq) return
    contentItems.value = res.results.map((r) => {
      const shortId = r.conversation_id.split(':').slice(2).join(':') || r.conversation_id
      const title = (r.title ?? '').trim() || shortId
      return {
        key: `hit:${r.conversation_id}`,
        title,
        subtitle: r.snippet,
        icon: Search,
        search: `${title} ${r.snippet ?? ''}`,
        run: () =>
          emit('open-session', {
            id: r.conversation_id,
            mode: 'agent',
            title,
            createdAt: Date.now(),
            updatedAt: Date.now()
          } as SessionListItem)
      }
    })
  } catch {
    if (seq === searchSeq) contentItems.value = []
  }
}

watch(query, (q) => {
  if (searchTimer) window.clearTimeout(searchTimer)
  const keyword = q.trim()
  if (keyword.length < 2) {
    contentItems.value = []
    return
  }
  searchTimer = window.setTimeout(() => void runContentSearch(keyword), 300)
})

const allGroups = computed<Array<{ name: string; items: PaletteItem[] }>>(() => [
  { name: '命令', items: commandItems },
  { name: '会话', items: sessionItems.value },
  { name: '会话内容', items: contentItems.value },
  { name: '知识库', items: wikiItems.value },
  { name: '能力市场', items: marketItems.value }
])

/** 纯本地过滤:空查询命令组全显示、其余组至多 8 条;有查询时每组至多 8 条并带上总数 */
const groups = computed<PaletteGroupView[]>(() => {
  const q = query.value.trim().toLowerCase()
  const filtering = q.length > 0
  return allGroups.value.map((group) => {
    const matched = filtering ? group.items.filter((item) => matchText(item, q)) : group.items
    const items = !filtering && group.name === '命令' ? matched : matched.slice(0, MAX_PER_GROUP)
    return { name: group.name, total: matched.length, items }
  })
})

function matchText(item: PaletteItem, q: string): boolean {
  return (item.search ?? item.title).toLowerCase().includes(q)
}

/** 可见条目的扁平顺序,供 ↑/↓ 循环导航 */
const flat = computed<PaletteItem[]>(() => groups.value.flatMap((group) => group.items))

// 过滤结果变化时,若当前高亮项消失则回落到首项
watch(flat, (list) => {
  if (!list.some((item) => item.key === activeKey.value)) activeKey.value = list[0]?.key ?? ''
})

function move(delta: number): void {
  const list = flat.value
  if (!list.length) return
  const idx = list.findIndex((item) => item.key === activeKey.value)
  const next = idx < 0 ? 0 : (idx + delta + list.length) % list.length
  activeKey.value = list[next].key
}

/** 执行指定条目,缺省执行当前高亮项,随后关闭面板 */
function run(item?: PaletteItem): void {
  const target = item ?? flat.value.find((it) => it.key === activeKey.value)
  if (!target) return
  close()
  target.run()
}

function close(): void {
  emit('update:modelValue', false)
}

// 打开时:清空查询、聚焦输入框;知识库/市场各只拉取一次
watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    query.value = ''
    activeKey.value = flat.value[0]?.key ?? ''
    if (!wikiLoaded.value && !wikiLoading.value) void loadWiki()
    if (!marketLoaded.value && !marketLoading.value) void loadMarket()
    void nextTick(() => inputRef.value?.focus())
  },
  { immediate: true }
)

function formatTime(t: number): string {
  const d = new Date(t)
  if (Number.isNaN(d.getTime())) return ''
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  return sameDay
    ? d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
}
</script>

<template>
  <el-dialog
    :model-value="props.modelValue"
    :show-close="false"
    width="560px"
    top="12vh"
    class="cmd-palette"
    append-to-body
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-input
      ref="inputRef"
      v-model="query"
      :prefix-icon="Search"
      placeholder="搜索会话、知识库、能力，或输入命令…"
      clearable
      @keydown.down.prevent="move(1)"
      @keydown.up.prevent="move(-1)"
      @keydown.enter.prevent="run()"
      @keydown.esc="close()"
    />
    <div v-if="flat.length" class="cmd-list">
      <template v-for="group in groups" :key="group.name">
        <div v-if="group.items.length" class="cmd-group">
          <div class="cmd-group-title">
            {{ group.name }}
            <span v-if="query.trim()" class="cmd-group-count">{{ group.total }}</span>
          </div>
          <button
            v-for="item in group.items"
            :key="item.key"
            type="button"
            class="cmd-item"
            :class="{ active: item.key === activeKey }"
            @mouseenter="activeKey = item.key"
            @click="run(item)"
          >
            <el-icon v-if="item.icon"><component :is="item.icon" /></el-icon>
            <span class="cmd-item-title">{{ item.title }}</span>
            <span v-if="item.subtitle" class="cmd-item-sub">{{ item.subtitle }}</span>
          </button>
        </div>
      </template>
    </div>
    <el-empty v-else description="没有匹配结果" :image-size="60" />
  </el-dialog>
</template>

<style>
/* 面板 teleport 到 body,覆盖 el-dialog 默认头部留白,类名统一加 cmd- 前缀避免全局污染 */
.cmd-palette .el-dialog__header {
  display: none;
}

.cmd-palette .el-dialog__body {
  padding: 14px 14px 8px;
}

.cmd-list {
  margin-top: 10px;
  max-height: 52vh;
  overflow-y: auto;
}

.cmd-group {
  margin-bottom: 6px;
}

.cmd-group-title {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px 4px;
  font-size: 11px;
  color: var(--el-text-color-secondary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.cmd-group-count {
  font-size: 10.5px;
  color: var(--el-text-color-secondary);
}

.cmd-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  border: none;
  background: transparent;
  color: var(--el-text-color-regular);
  padding: 9px 10px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
  text-align: left;
}

.cmd-item.active {
  background: var(--el-fill-color-darker);
  color: var(--el-text-color-primary);
}

.cmd-item-title {
  flex: none;
}

.cmd-item-sub {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: right;
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
}
</style>
