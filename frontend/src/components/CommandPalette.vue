<script setup lang="ts">
import { computed, nextTick, ref, watch, type Component } from 'vue'
import {
  ChatDotRound, Clock, Collection, Document, MagicStick, Search, Setting, SwitchButton
} from '@element-plus/icons-vue'
import { api } from '../api'

/** 命令面板(Ctrl+K):本地过滤命令/会话/知识库/能力市场 + 会话内容检索。 */
const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'navigate', path: string): void
  (e: 'open-session', id: string): void
  (e: 'new-task'): void
}>()

interface PaletteItem {
  key: string
  title: string
  subtitle?: string
  icon?: Component
  search?: string
  run: () => void
}
interface PaletteGroup {
  name: string
  total: number
  items: PaletteItem[]
}

const MAX_PER_GROUP = 8
const query = ref('')
const inputRef = ref<{ focus: () => void } | null>(null)
const activeKey = ref('')

const commandItems: PaletteItem[] = [
  { key: 'cmd:chat', title: '对话', icon: ChatDotRound, run: () => emit('navigate', '/chat') },
  { key: 'cmd:sessions', title: '会话历史', icon: Clock, run: () => emit('navigate', '/sessions') },
  { key: 'cmd:knowledge', title: '知识库', icon: Collection, run: () => emit('navigate', '/knowledge') },
  { key: 'cmd:market', title: '能力市场', icon: MagicStick, run: () => emit('navigate', '/market') },
  { key: 'cmd:settings', title: '设置', icon: Setting, run: () => emit('navigate', '/settings') },
  { key: 'cmd:new', title: '新建任务', icon: SwitchButton, search: '新建任务 new task', run: () => emit('new-task') }
]

const sessionItems = ref<PaletteItem[]>([])
async function loadSessions(): Promise<void> {
  try {
    const [hist, live] = await Promise.all([
      api<{ sessions: any[] }>('/api/agent/sessions/history?limit=200').catch(() => ({ sessions: [] })),
      api<{ sessions: any[] }>('/api/sessions').catch(() => ({ sessions: [] }))
    ])
    const map = new Map<string, { id: string; channel: string; at?: string }>()
    for (const h of hist.sessions ?? []) {
      if (h.id) map.set(h.id, { id: h.id, channel: h.channel || 'other', at: h.last_accessed || h.first_accessed })
    }
    for (const l of live.sessions ?? []) {
      if (l.id) {
        const prev = map.get(l.id)
        map.set(l.id, { id: l.id, channel: prev?.channel || 'web', at: prev?.at || l.created_at })
      }
    }
    sessionItems.value = Array.from(map.values())
      .sort((a, b) => String(b.at || '').localeCompare(String(a.at || '')))
      .map((s) => ({
        key: `session:${s.id}`,
        title: s.id,
        subtitle: `${s.channel} · ${fmtTime(s.at)}`,
        icon: ChatDotRound,
        search: s.id,
        run: () => emit('open-session', s.id)
      }))
  } catch {
    sessionItems.value = []
  }
}

const wikiItems = ref<PaletteItem[]>([])
let wikiLoaded = false
let wikiLoading = false
async function loadWiki(): Promise<void> {
  wikiLoading = true
  try {
    const data = await api<{ categories: Array<{ name: string; pages: any[] }> }>('/api/knowledge/wiki')
    const items: PaletteItem[] = []
    for (const cat of data.categories ?? []) {
      for (const p of cat.pages ?? []) {
        items.push({
          key: `wiki:${p.id}`,
          title: p.title,
          subtitle: cat.name,
          icon: Document,
          search: `${p.title} ${p.summary ?? ''} ${p.id}`,
          run: () => emit('navigate', '/knowledge')
        })
      }
    }
    wikiItems.value = items
  } catch {
    wikiItems.value = []
  } finally {
    wikiLoaded = true
    wikiLoading = false
  }
}

const marketItems = ref<PaletteItem[]>([])
let marketLoaded = false
let marketLoading = false
async function loadMarket(): Promise<void> {
  marketLoading = true
  try {
    const data = await api<{ items: any[] }>('/api/market/capabilities?page_size=100')
    const items: PaletteItem[] = []
    for (const it of data.items ?? []) {
      const name = typeof it?.name === 'string' ? it.name : ''
      if (!name) continue
      items.push({
        key: `market:${it.id || name}`,
        title: name,
        subtitle: typeof it.description === 'string' ? it.description : undefined,
        icon: MagicStick,
        search: `${name} ${it.description ?? ''} ${it.id ?? ''}`,
        run: () => emit('navigate', '/market')
      })
    }
    marketItems.value = items
  } catch {
    marketItems.value = []
  } finally {
    marketLoaded = true
    marketLoading = false
  }
}

const contentItems = ref<PaletteItem[]>([])
let searchTimer: number | undefined
let searchSeq = 0
async function runContentSearch(keyword: string): Promise<void> {
  const seq = ++searchSeq
  try {
    const res = await api<{ results: any[] }>(
      `/api/agent/sessions/search?q=${encodeURIComponent(keyword)}&limit=20`
    )
    if (seq !== searchSeq) return
    contentItems.value = (res.results ?? []).map((r) => {
      const cid = String(r.conversation_id || '')
      const shortId = cid.split(':').slice(2).join(':') || cid
      const title = String(r.title ?? '').trim() || shortId
      return {
        key: `hit:${cid}`,
        title,
        subtitle: r.snippet,
        icon: Search,
        search: `${title} ${r.snippet ?? ''}`,
        run: () => emit('open-session', cid)
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

const groups = computed<PaletteGroup[]>(() => {
  const q = query.value.trim().toLowerCase()
  const filtering = q.length > 0
  return allGroups.value.map((group) => {
    const matched = filtering ? group.items.filter((it) => (it.search ?? it.title).toLowerCase().includes(q)) : group.items
    const items = !filtering && group.name === '命令' ? matched : matched.slice(0, MAX_PER_GROUP)
    return { name: group.name, total: matched.length, items }
  })
})

const flat = computed<PaletteItem[]>(() => groups.value.flatMap((g) => g.items))
watch(flat, (list) => {
  if (!list.some((it) => it.key === activeKey.value)) activeKey.value = list[0]?.key ?? ''
})

function move(delta: number): void {
  const list = flat.value
  if (!list.length) return
  const idx = list.findIndex((it) => it.key === activeKey.value)
  const next = idx < 0 ? 0 : (idx + delta + list.length) % list.length
  activeKey.value = list[next].key
}

function run(item?: PaletteItem): void {
  const target = item ?? flat.value.find((it) => it.key === activeKey.value)
  if (!target) return
  close()
  target.run()
}

function close(): void {
  emit('update:modelValue', false)
}

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    query.value = ''
    activeKey.value = flat.value[0]?.key ?? ''
    void loadSessions()
    if (!wikiLoaded && !wikiLoading) void loadWiki()
    if (!marketLoaded && !marketLoading) void loadMarket()
    void nextTick(() => inputRef.value?.focus())
  },
  { immediate: true }
)

function fmtTime(t?: string): string {
  if (!t) return ''
  const d = new Date(t)
  return Number.isNaN(d.getTime())
    ? String(t)
    : d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
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
.cmd-palette .el-dialog__header { display: none; }
.cmd-palette .el-dialog__body { padding: 14px 14px 8px; }
.cmd-list { margin-top: 10px; max-height: 52vh; overflow-y: auto; }
.cmd-group { margin-bottom: 6px; }
.cmd-group-title {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 8px 4px; font-size: 11px;
  color: var(--el-text-color-secondary);
  text-transform: uppercase; letter-spacing: 0.05em;
}
.cmd-group-count { font-size: 10.5px; color: var(--el-text-color-secondary); }
.cmd-item {
  display: flex; align-items: center; gap: 10px; width: 100%;
  border: none; background: transparent;
  color: var(--el-text-color-regular);
  padding: 9px 10px; border-radius: 8px; cursor: pointer;
  font-size: 13px; text-align: left;
}
.cmd-item.active { background: var(--el-fill-color-darker); color: var(--el-text-color-primary); }
.cmd-item-title { flex: none; }
.cmd-item-sub {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  text-align: right; font-size: 11.5px; color: var(--el-text-color-secondary);
}
</style>
