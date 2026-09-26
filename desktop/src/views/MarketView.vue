<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { ApiError, request } from '../api/client'
import type {
  CapabilityRuntime,
  MarketCapabilityLite,
  MarketCapabilityType,
  MarketInstalledItem
} from '../api/types'
import { useSettingsStore } from '../stores/settings'
import {
  distBadgeCls,
  distName,
  gradeOf,
  iconPhStyle,
  policyName,
  typeLetter,
  typeName
} from '../utils/market'

const router = useRouter()
const settings = useSettingsStore()

const tab = ref<'browse' | 'installed'>('browse')

/** portal /api/capabilities 分页外壳(只读最小字段,容错) */
interface MarketCapPage {
  items?: unknown[]
  total?: number
}

/** 本地模式支持订阅/安装的四种类型标签 */
const TYPE_LABEL: Record<MarketCapabilityType, string> = {
  agent: '智能体',
  tool: '工具',
  skill: '技能',
  mcp: 'MCP'
}

/** 本地执行命令预览(安装本地 stdio MCP 时由主进程返回) */
interface InstallConsent {
  command: string
  args: string[]
  envKeys: string[]
  cwd?: string
}

/** IPC 返回的 {ok, output} 形状(subscribe/uninstall/install 通用); install 可带 consent 预览 */
interface OpResult {
  ok?: boolean
  output?: string
  consent?: InstallConsent
}

// ---- 浏览页状态(与 KnowledgeView 同风格:加载骨架/错误重试/空态) ----
const browseLoading = ref(false)
const browseLoaded = ref(false)
const browseError = ref('')
const browse = ref<MarketCapabilityLite[]>([])

// ---- 浏览页筛选(搜索 + 类型 + 分类) ----
const q = ref('')
const typeFilter = ref<'' | MarketCapabilityType>('')
const categoryFilter = ref('')
/** 分类选项：取自当前目录出现过的分类 */
const categoryOptions = computed(() => {
  const set = new Set<string>()
  for (const c of browse.value) if (c.category) set.add(c.category)
  return Array.from(set).sort()
})

// ---- 已安装页状态 ----
const installedLoading = ref(false)
const installedLoaded = ref(false)
const installedError = ref('')
const installed = ref<MarketInstalledItem[]>([])

/** 我的能力 id 集合(listMy added,用于对拍浏览卡片的订阅态) */
const mineIds = ref<Set<string>>(new Set())

// 细粒度动作 loading:分别锁住当前卡片的按钮,避免整页抖动
const busySub = ref('')
const busyInstall = ref('')
const busyUninstall = ref('')

function typeKey(type: MarketCapabilityType, name: string): string {
  return `${type}/${name}`
}

/** 去掉 Electron invoke 错误的外层包装("Error invoking remote method ...: Error: ...") */
function invokeErr(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err)
  return raw.replace(/^Error invoking remote method '[^']*':\s*(Error:\s*)?/, '')
}

function marketErr(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return '访问能力市场授权失败(401),请重新完成企业 SSO 登录'
    if (err.status === 403) return '能力市场拒绝了当前账号(403),请确认账号已开通市场访问'
    return `能力市场请求失败(HTTP ${err.status}):${err.message}`
  }
  return `能力市场不可达:${invokeErr(err)}`
}

/** runtime 容错映射:只收消费字段(cloud/local/recommended);非法/空对象返回 undefined */
function mapRuntime(v: unknown): CapabilityRuntime | undefined {
  if (typeof v !== 'object' || v === null || Array.isArray(v)) return undefined
  const r = v as Record<string, unknown>
  const out: CapabilityRuntime = {}
  if (typeof r.cloud === 'boolean') out.cloud = r.cloud
  if (typeof r.local === 'boolean') out.local = r.local
  if (r.recommended === 'cloud' || r.recommended === 'local') out.recommended = r.recommended
  return Object.keys(out).length ? out : undefined
}

/** portal 单条能力容错映射:缺关键字段/非本地四类(如 workflow)返回 null 跳过 */
function mapLite(v: unknown): MarketCapabilityLite | null {
  if (typeof v !== 'object' || v === null) return null
  const r = v as Record<string, unknown>
  const id = typeof r.id === 'string' && r.id ? r.id : ''
  const name = typeof r.name === 'string' && r.name ? r.name : ''
  const version = typeof r.version === 'string' && r.version ? r.version : ''
  const type = r.type
  if (!id || !name || !version) return null
  if (type !== 'agent' && type !== 'tool' && type !== 'skill' && type !== 'mcp') return null
  const out: MarketCapabilityLite = { id, name, type, version }
  const desc = typeof r.description === 'string' && r.description ? r.description : ''
  if (desc) out.description = desc
  const author = typeof r.author_name === 'string' && r.author_name ? r.author_name : ''
  if (author) out.author_name = author
  const category = typeof r.category === 'string' && r.category ? r.category : ''
  if (category) out.category = category
  const distribution = typeof r.distribution === 'string' && r.distribution ? r.distribution : ''
  if (distribution) out.distribution = distribution
  const policy = typeof r.install_policy === 'string' ? r.install_policy : ''
  if (policy) out.install_policy = policy
  const runtime = mapRuntime(r.runtime)
  if (runtime) out.runtime = runtime
  const ratingCount = Number(r.rating_count ?? 0)
  if (Number.isFinite(ratingCount) && ratingCount > 0) {
    out.rating_count = Math.floor(ratingCount)
    const avg = Number(r.avg_rating ?? 0)
    if (Number.isFinite(avg) && avg > 0) out.avg_rating = Math.round(avg * 10) / 10
  }
  return out
}

/** 拉取完整目录:portal 默认只回最新版本并分页,这里按 page_size=100 翻页取全(上限 2000 项兜底) */
async function fetchBrowseDirectory(): Promise<MarketCapabilityLite[]> {
  const PAGE_SIZE = 100
  const all: MarketCapabilityLite[] = []
  for (let page = 1; page <= 20; page++) {
    const data = await request<MarketCapPage>(
      'market',
      `/api/capabilities?page_size=${PAGE_SIZE}&page=${page}`
    )
    const items = Array.isArray(data?.items) ? data.items : []
    for (const raw of items) {
      const cap = mapLite(raw)
      if (cap) all.push(cap)
    }
    const total = typeof data?.total === 'number' ? data.total : all.length
    if (all.length >= total || items.length === 0) break
  }
  // 按 (type,name) 去重:同版本去重后的首个即为当前版本(服务端已按最新版折叠,这里兜底)
  const seen = new Set<string>()
  const out: MarketCapabilityLite[] = []
  for (const cap of all) {
    const key = typeKey(cap.type, cap.name)
    if (seen.has(key)) continue
    seen.add(key)
    out.push(cap)
  }
  return out
}

/** 拉取「我的能力」id 集合;失败时保留旧值,talk=true 时报错提示 */
async function loadMine(talk = false): Promise<void> {
  try {
    const list = await window.desktop.invoke<Array<{ id?: unknown }>>('localagent:market:listMy')
    const ids: string[] = []
    for (const item of Array.isArray(list) ? list : []) {
      if (item && typeof item.id === 'string' && item.id) ids.push(item.id)
    }
    mineIds.value = new Set(ids)
  } catch (err) {
    if (talk) ElMessage.error(`刷新「我的能力」失败:${invokeErr(err)}`)
  }
}

async function fetchInstalled(): Promise<MarketInstalledItem[]> {
  const list = await window.desktop.invoke<MarketInstalledItem[]>(
    'localagent:market:listInstalled'
  )
  return Array.isArray(list) ? list : []
}

/** 已安装列表(首次/重试/刷新共用;带错误态与加载态) */
async function loadInstalled(): Promise<void> {
  if (!settings.hasUser) return
  installedError.value = ''
  installedLoading.value = true
  try {
    installed.value = await fetchInstalled()
    installedLoaded.value = true
  } catch (err) {
    installedError.value = invokeErr(err)
  } finally {
    installedLoading.value = false
  }
}

/** 浏览页加载:目录为硬依赖,listMy/listInstalled 独立降级不阻断浏览 */
async function refreshBrowse(): Promise<void> {
  if (!settings.hasUser) return
  browseError.value = ''
  browseLoading.value = true
  try {
    browse.value = await fetchBrowseDirectory()
    browseLoaded.value = true
    void loadIcons()
  } catch (err) {
    browseError.value = marketErr(err)
  } finally {
    browseLoading.value = false
  }
  await Promise.allSettled([loadMine(), loadInstalled()])
}

/** 头部刷新:按当前页签只刷新对应数据 */
function onRefresh(): void {
  if (tab.value === 'browse') void refreshBrowse()
  else void loadInstalled()
}

function onTab(next: 'browse' | 'installed'): void {
  tab.value = next
  if (next === 'browse') {
    if (!browseLoaded.value && !browseLoading.value) void refreshBrowse()
  } else if (!installedLoaded.value && !installedLoading.value) {
    void loadInstalled()
  }
}

const countText = computed(() => {
  if (tab.value === 'browse') {
    if (!browseLoaded.value) return ''
    return hasBrowseFilter.value ? `筛选 ${cards.value.length} / ${browse.value.length} 项` : `共 ${browse.value.length} 项`
  }
  return installedLoaded.value ? `已安装 ${installed.value.length} 项` : ''
})

function ratingText(c: MarketCapabilityLite): string {
  const count = typeof c.rating_count === 'number' ? c.rating_count : 0
  const avg = typeof c.avg_rating === 'number' && count > 0 ? c.avg_rating : 0
  return avg ? `★ ${avg.toFixed(1)}(${count})` : `★(${count})`
}

interface BrowseCard extends MarketCapabilityLite {
  mine: boolean
  installed: boolean
}

/** 浏览卡片 = 目录条目(先按 搜索/类型/分类 过滤) + 订阅态 + 本机已装态 */
const cards = computed<BrowseCard[]>(() => {
  const kw = q.value.trim().toLowerCase()
  const tf = typeFilter.value
  const cf = categoryFilter.value
  const filtered = browse.value.filter((c) => {
    if (tf && c.type !== tf) return false
    if (cf && c.category !== cf) return false
    if (kw) {
      const hay = `${c.name} ${c.description ?? ''} ${c.category ?? ''} ${c.author_name ?? ''}`.toLowerCase()
      if (!hay.includes(kw)) return false
    }
    return true
  })
  const mine = mineIds.value
  const instKeys = new Set(installed.value.map((it) => typeKey(it.type, it.name)))
  return filtered.map((c) => ({
    ...c,
    mine: mine.has(c.id),
    installed: instKeys.has(typeKey(c.type, c.name))
  }))
})

/** 是否有生效的浏览筛选 */
const hasBrowseFilter = computed(() => !!(q.value.trim() || typeFilter.value || categoryFilter.value))
function resetBrowseFilter(): void {
  q.value = ''
  typeFilter.value = ''
  categoryFilter.value = ''
}

/** 能力图标 id → data URL（懒加载；无图标为空） */
const iconMap = ref<Record<string, string>>({})
function iconOf(id: string): string {
  return iconMap.value[id] || ''
}
async function loadIcons(): Promise<void> {
  for (const c of browse.value) {
    if (iconMap.value[c.id] !== undefined) continue
    try {
      const url = await window.desktop.invoke<string>('localagent:market:icon', { id: c.id })
      iconMap.value = { ...iconMap.value, [c.id]: url || '' }
    } catch {
      iconMap.value = { ...iconMap.value, [c.id]: '' }
    }
  }
}

async function subscribeCap(card: BrowseCard): Promise<void> {
  busySub.value = card.id
  try {
    const res = await window.desktop.invoke<OpResult>('localagent:market:subscribe', {
      capabilityId: card.id
    })
    ElMessage.success(res?.output || '已加入我的能力')
    await loadMine(true)
  } catch (err) {
    ElMessage.error(`加入失败:${invokeErr(err)}`)
  } finally {
    busySub.value = ''
  }
}

async function unsubscribeCap(card: BrowseCard): Promise<void> {
  busySub.value = card.id
  try {
    const res = await window.desktop.invoke<OpResult>('localagent:market:unsubscribe', {
      capabilityId: card.id
    })
    ElMessage.success(res?.output || '已从我的能力移除')
    await loadMine(true)
  } catch (err) {
    ElMessage.error(`移出失败:${invokeErr(err)}`)
  } finally {
    busySub.value = ''
  }
}

/** 安装类型附加提示(与主进程 InstallResult.output 拼一起展示) */
function installNote(type: MarketCapabilityType): string {
  if (type === 'skill') return '新技能在本地模式会话可用'
  if (type === 'mcp') return '下一轮对话自动生效(无需重启)'
  return ''
}

/** 安装方式能力:runtime 优先(按维度),缺维度回退 distribution;异常全 false 按缺失处理 */
function installSupport(cap: {
  runtime?: CapabilityRuntime
  distribution?: string
}): { cloud: boolean; local: boolean; recommended?: 'cloud' | 'local' } {
  const rt = cap.runtime
  const rtCloud = typeof rt?.cloud === 'boolean' ? rt.cloud : null
  const rtLocal = typeof rt?.local === 'boolean' ? rt.local : null
  const runtimeUsable = !(rtCloud === false && rtLocal === false) && (rtCloud !== null || rtLocal !== null)
  if (runtimeUsable) {
    return {
      cloud: rtCloud ?? cap.distribution !== 'local',
      local: rtLocal ?? cap.distribution !== 'remote',
      recommended: rt?.recommended
    }
  }
  return {
    cloud: cap.distribution !== 'local',
    local: cap.distribution !== 'remote'
  }
}

/** MCP 分发方式徽标文案:runtime.cloud/local 优先,缺失回退 distribution;both/缺失视为双模式 */
function distLabel(cap: { runtime?: CapabilityRuntime; distribution?: string }): string {
  const { cloud, local } = installSupport(cap)
  if (cloud && !local) return '仅云端托管'
  if (local && !cloud) return '仅本地'
  return '双模式'
}

/** 已安装 MCP 条目的当前模式文案 */
function modeLabel(mode?: 'platform' | 'local'): string {
  return mode === 'platform' ? '云端托管' : '本地安装'
}

/** 双可安装时弹窗二选一:按 recommended 决定主按钮(文案不变),关闭视为放弃 */
async function pickInstallMode(
  card: BrowseCard,
  recommended?: 'cloud' | 'local'
): Promise<'platform' | 'local' | null> {
  const preferLocal = recommended === 'local'
  try {
    await ElMessageBox.confirm(
      `「${card.name}」支持两种运行方式：\n云端托管（经平台 MCP 网关调用，无需本机环境）\n本地安装（由本机直接拉起，依赖本机运行环境）`,
      '选择安装方式',
      {
        type: 'info',
        confirmButtonText: preferLocal ? '本地安装' : '云端托管',
        cancelButtonText: preferLocal ? '云端托管' : '本地安装',
        distinguishCancelAndClose: true
      }
    )
    return preferLocal ? 'local' : 'platform'
  } catch (action) {
    if (action !== 'cancel') return null
    return preferLocal ? 'platform' : 'local'
  }
}

/**
 * 选择 MCP 安装模式:runtime 优先(cloud&&!local→云端托管、local&&!cloud→本地安装、
 * 双可弹窗按 recommended 高亮);runtime 缺失回退 distribution(remote→云端、local→本地、
 * both/缺失→弹窗)。
 */
async function chooseMcpMode(card: BrowseCard): Promise<'platform' | 'local' | null> {
  const support = installSupport(card)
  if (support.cloud && !support.local) return 'platform'
  if (support.local && !support.cloud) return 'local'
  return await pickInstallMode(card, support.recommended)
}

function showResult(title: string, output?: string, note?: string): void {
  const body = note && output ? `${output}\n\n${note}` : note || output || '操作成功'
  if (body.length > 100) {
    void ElMessageBox.alert(body, title, { type: 'success', confirmButtonText: '知道了' })
  } else {
    ElMessage.success(body)
  }
}

async function doInstall(card: BrowseCard): Promise<void> {
  busyInstall.value = card.name
  try {
    // mcp：按分发方式选中安装模式（双模式弹选择），取消则不安装
    let mode: 'platform' | 'local' | undefined
    if (card.type === 'mcp') {
      const picked = await chooseMcpMode(card)
      if (!picked) return
      mode = picked
    }
    const payload: { name: string; mode?: 'platform' | 'local'; confirmed?: boolean } = { name: card.name }
    if (mode) payload.mode = mode
    let res = await window.desktop.invoke<OpResult>('localagent:market:install', payload)
    // 本地 stdio MCP 会在本机执行命令：先展示完整 command/args 由用户显式确认，再带 confirmed 重试
    if (res && res.ok === false && res.consent) {
      const c = res.consent
      const cmdText = [c.command, ...c.args].join(' ')
      const detail =
        `该连接器将在本机执行以下命令：\n\n${cmdText}` +
        (c.cwd ? `\n\n工作目录：${c.cwd}` : '') +
        (c.envKeys.length ? `\n\n环境变量：${c.envKeys.join(', ')}` : '') +
        '\n\n仅当你信任该能力来源时继续。'
      try {
        await ElMessageBox.confirm(detail, '本地执行命令确认', {
          type: 'warning',
          confirmButtonText: '信任并安装',
          cancelButtonText: '取消',
          customClass: 'market-consent-box'
        })
      } catch {
        return // 用户取消
      }
      payload.confirmed = true
      res = await window.desktop.invoke<OpResult>('localagent:market:install', payload)
    }
    if (!res || res.ok === false) throw new Error(res?.output || '安装失败')
    // tool 走远程适配(云上注册 market:<name>);skill/mcp/agent 本地落盘,按类型附生效提示
    showResult(`安装「${TYPE_LABEL[card.type]} ${card.name}」成功`, res.output, installNote(card.type))
    await Promise.all([loadMine(true), loadInstalled()])
  } catch (err) {
    ElMessage.error(`安装失败:${invokeErr(err)}`)
  } finally {
    busyInstall.value = ''
  }
}

async function uninstallItem(item: MarketInstalledItem): Promise<void> {
  const key = typeKey(item.type, item.name)
  try {
    await ElMessageBox.confirm(
      `确认卸载「${TYPE_LABEL[item.type]} ${item.name}${item.version ? ' v' + item.version : ''}」?本地文件/条目将被移除。`,
      '卸载能力',
      { type: 'warning', confirmButtonText: '卸载', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  busyUninstall.value = key
  try {
    const res = await window.desktop.invoke<OpResult>('localagent:market:uninstall', {
      type: item.type,
      name: item.name
    })
    if (!res || res.ok === false) throw new Error(res?.output || '卸载失败')
    const note = item.type === 'mcp' ? 'MCP 连接已重置,下一轮对话自动生效(无需重启)' : ''
    showResult('卸载成功', res.output, note)
    await loadInstalled()
  } catch (err) {
    ElMessage.error(`卸载失败:${invokeErr(err)}`)
  } finally {
    busyUninstall.value = ''
  }
}

const headerLoading = computed(() =>
  tab.value === 'browse' ? browseLoading.value : installedLoading.value
)

onMounted(() => {
  if (settings.hasUser) void refreshBrowse()
})
</script>

<template>
  <div class="market-view">
    <header class="view-header">
      <div class="view-header-left">
        <span class="view-title">能力市场</span>
        <span class="market-count">{{ countText }}</span>
      </div>
      <el-button
        :icon="Refresh"
        size="small"
        :loading="headerLoading"
        :disabled="!settings.hasUser"
        @click="onRefresh"
      >
        刷新
      </el-button>
    </header>

    <nav class="market-tabs">
      <button class="market-tab" :class="{ active: tab === 'browse' }" @click="onTab('browse')">
        浏览
      </button>
      <button
        class="market-tab"
        :class="{ active: tab === 'installed' }"
        @click="onTab('installed')"
      >
        已安装
      </button>
    </nav>

    <!-- 未登录:先完成企业 SSO 登录 -->
    <div v-if="!settings.hasUser" class="market-body">
      <div class="market-state">
        <el-empty description="登录后可浏览与安装能力">
          <el-button type="primary" size="small" @click="router.push('/settings')">前往登录</el-button>
        </el-empty>
      </div>
    </div>

    <!-- 浏览:市场目录卡片 -->
    <div v-else-if="tab === 'browse'" class="market-body">
      <section class="market-pane">
        <div class="market-filters">
          <el-input
            v-model="q"
            placeholder="搜索名称 / 描述 / 分类"
            clearable
            size="small"
            style="width: 240px"
          />
          <el-select v-model="typeFilter" size="small" style="width: 130px">
            <el-option label="全部类型" value="" />
            <el-option label="智能体" value="agent" />
            <el-option label="工具" value="tool" />
            <el-option label="技能" value="skill" />
            <el-option label="MCP" value="mcp" />
          </el-select>
          <el-select
            v-model="categoryFilter"
            size="small"
            style="width: 160px"
            placeholder="全部分类"
            clearable
          >
            <el-option label="全部分类" value="" />
            <el-option v-for="c in categoryOptions" :key="c" :label="c" :value="c" />
          </el-select>
          <el-button v-if="hasBrowseFilter" size="small" text @click="resetBrowseFilter">
            重置
          </el-button>
        </div>
        <el-skeleton
          v-if="browseLoading && !browseLoaded"
          :rows="7"
          animated
          class="market-skeleton"
        />
        <template v-else>
          <div v-if="browseError" class="market-state">
            <el-empty :description="browseError">
              <el-button size="small" @click="refreshBrowse">重试</el-button>
            </el-empty>
          </div>
          <div v-else-if="browseLoaded && cards.length === 0" class="market-state">
            <el-empty :description="hasBrowseFilter ? '没有匹配的能力，试试清空筛选' : '市场还没有可显示内容'" />
          </div>
          <div v-else-if="browseLoaded" v-loading="browseLoading" class="market-grid">
            <article v-for="card in cards" :key="card.id" class="market-card">
              <div class="market-card-head">
                <img
                  v-if="iconOf(card.id)"
                  class="market-icon"
                  :src="iconOf(card.id)"
                  :alt="card.name"
                />
                <span v-else class="market-icon market-icon-ph" :style="iconPhStyle(card.type)">
                  {{ typeLetter(card.type) }}
                </span>
                <div class="market-head-text">
                  <h3 class="market-name" :title="card.name">{{ card.name }}</h3>
                  <div class="market-sub">
                    {{ typeName(card.type) }}<span v-if="card.version"> · v{{ card.version }}</span>
                  </div>
                </div>
              </div>
              <p class="market-desc">{{ card.description || '暂无描述' }}</p>
              <div class="market-tags">
                <span class="badge" :class="gradeOf(card).cls">{{ gradeOf(card).label }}</span>
                <span class="badge" :class="distBadgeCls(card.distribution)">{{ distName(card.distribution) }}</span>
                <span v-if="card.category" class="badge">{{ card.category }}</span>
                <span v-if="(card.install_policy || 'optional') !== 'optional'" class="badge badge-warning">
                  {{ policyName(card.install_policy) }}
                </span>
              </div>
              <div class="market-card-foot">
                <span class="market-foot-meta">
                  <span v-if="card.rating_count" class="market-rating" title="评分">{{ ratingText(card) }}</span>
                  <span v-else class="market-muted">暂无评分</span>
                  <span v-if="card.usage_count"> · {{ card.usage_count }} 次</span>
                </span>
                <span class="market-spacer"></span>
                <el-tag v-if="card.mine" size="small" type="success" effect="plain">我的</el-tag>
                <el-button
                  size="small"
                  type="primary"
                  :loading="busyInstall === card.name"
                  :disabled="!card.mine || !!busyInstall || busySub === card.id"
                  :title="!card.mine ? '需先加入后再安装' : ''"
                  @click="doInstall(card)"
                >
                  {{ card.installed ? '已安装' : '安装' }}
                </el-button>
                <el-button
                  size="small"
                  :type="card.mine ? 'danger' : 'primary'"
                  plain
                  :loading="busySub === card.id"
                  :disabled="!!busyInstall || busyUninstall !== ''"
                  @click="card.mine ? unsubscribeCap(card) : subscribeCap(card)"
                >
                  {{ card.mine ? '移出' : '加入' }}
                </el-button>
              </div>
            </article>
          </div>
        </template>
      </section>
    </div>

    <!-- 已安装:本地能力清单 -->
    <div v-else class="market-body">
      <section class="market-pane">
        <el-skeleton
          v-if="installedLoading && !installedLoaded"
          :rows="7"
          animated
          class="market-skeleton"
        />
        <template v-else>
          <div v-if="installedError" class="market-state">
            <el-empty :description="installedError">
              <el-button size="small" @click="loadInstalled">重试</el-button>
            </el-empty>
          </div>
          <div v-else-if="installedLoaded && installed.length === 0" class="market-state">
            <el-empty description="本地还没有安装任何能力" />
          </div>
          <div v-else-if="installedLoaded" v-loading="installedLoading" class="market-installed">
            <div v-for="item in installed" :key="typeKey(item.type, item.name)" class="market-inst-row">
              <span class="market-type" :class="'t-' + item.type">{{ TYPE_LABEL[item.type] }}</span>
              <span v-if="item.type === 'mcp' && item.mode" class="market-dist">
                {{ modeLabel(item.mode) }}
              </span>
              <div class="market-inst-main">
                <div class="market-inst-name" :title="item.name">{{ item.name }}</div>
                <div v-if="item.description" class="market-inst-desc">{{ item.description }}</div>
              </div>
              <span class="market-inst-ver">{{ item.version ? 'v' + item.version : '—' }}</span>
              <el-button
                size="small"
                type="danger"
                plain
                :loading="busyUninstall === typeKey(item.type, item.name)"
                @click="uninstallItem(item)"
              >
                卸载
              </el-button>
            </div>
          </div>
        </template>
      </section>
    </div>
  </div>
</template>
