<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api, del, patch, post } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

interface MarketComponent {
  name?: string
  type?: string
  description?: string
  version?: string
}

interface MarketItem {
  id: string
  name: string
  type: string
  description?: string
  category?: string
  tags?: string[]
  version?: string
  distribution?: string
  components?: MarketComponent[]
  joined?: boolean
  installed?: boolean
}

interface Installation {
  capability_id: string
  capability_name: string
  kind: string
  enabled: boolean
}

const items = ref<MarketItem[]>([])
const installations = ref<Installation[]>([])
const total = ref(0)
const loading = ref(false)
const marketMissing = ref(false)

const query = reactive({ q: '', type: '', page: 1, page_size: 12 })

const typeOptions = [
  { value: '', label: '全部' },
  { value: 'mcp', label: '连接器' },
  { value: 'skill', label: '技能' },
  { value: 'agent', label: '专家' },
  { value: 'plugin', label: '能力包' }
]

const installedMap = computed(() => {
  const m = new Map<string, Installation>()
  for (const it of installations.value) m.set(it.capability_id, it)
  return m
})

function typeLabel(t: string): string {
  const map: Record<string, string> = {
    mcp: '连接器', skill: '技能', agent: '专家', plugin: '能力包',
    tool: '工具', rule: '规则', command: '命令', hook: '钩子'
  }
  return map[t] ?? (t || '未知')
}

function distLabel(d?: string): string {
  if (d === 'local') return '本地'
  if (d === 'remote') return '云端'
  return '本地+云端'
}

function distTagType(d?: string): 'info' | 'success' | 'warning' {
  if (d === 'local') return 'warning'
  if (d === 'remote') return 'success'
  return 'info'
}

/** 可安装到云端托管: 连接器(type=mcp) + 已加入 + 非本地分发 */
function canInstall(item: MarketItem): boolean {
  return item.type === 'mcp' && Boolean(item.joined) && item.distribution !== 'local'
}

function enabledOf(item: MarketItem): boolean {
  return installedMap.value.get(item.id)?.enabled ?? true
}

async function load() {
  loading.value = true
  marketMissing.value = false
  try {
    const qs = new URLSearchParams()
    if (query.q) qs.set('q', query.q)
    if (query.type) qs.set('type', query.type)
    qs.set('page', String(query.page))
    qs.set('page_size', String(query.page_size))
    const [browse, local] = await Promise.all([
      api<{ items: MarketItem[]; total: number }>(`/api/market/capabilities?${qs.toString()}`),
      api<{ installations: Installation[] }>('/api/market/installations').catch(() => ({ installations: [] }))
    ])
    items.value = browse.items ?? []
    total.value = browse.total ?? 0
    installations.value = local.installations ?? []
  } catch (e) {
    if ((e as { status?: number }).status === 503) {
      marketMissing.value = true
      items.value = []
      total.value = 0
    } else {
      ElMessage.error((e as Error).message)
    }
  } finally {
    loading.value = false
  }
}

function search() {
  query.page = 1
  void load()
}

function resetSearch() {
  query.q = ''
  query.type = ''
  query.page = 1
  void load()
}

// ---------------- 详情 ----------------
const detail = ref<MarketItem | null>(null)
const drawer = ref(false)
const detailLoading = ref(false)

async function openDetail(item: MarketItem) {
  drawer.value = true
  detailLoading.value = true
  detail.value = { ...item }
  try {
    const d = await api<MarketItem>(`/api/market/capabilities/${item.id}`)
    detail.value = d
  } catch (e) {
    if ((e as { status?: number }).status === 503) {
      marketMissing.value = true
    } else {
      ElMessage.error((e as Error).message)
    }
  } finally {
    detailLoading.value = false
  }
}

async function refreshAfterAction(id: string) {
  await load()
  const updated = items.value.find((i) => i.id === id)
  if (detail.value && detail.value.id === id && updated) {
    detail.value.joined = updated.joined
    detail.value.installed = updated.installed
  }
}

// ---------------- 操作 ----------------
async function join(item: MarketItem) {
  try {
    await post(`/api/market/capabilities/${item.id}/join`, {})
    ElMessage.success(`已加入「${item.name}」`)
    await refreshAfterAction(item.id)
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function leave(item: MarketItem) {
  try {
    await ElMessageBox.confirm(`确认从市场移出「${item.name}」? 已安装的云端托管需先卸载。`,
      '移出确认', { type: 'warning' })
  } catch { return }
  try {
    await post(`/api/market/capabilities/${item.id}/leave`, {})
    ElMessage.success(`已移出「${item.name}」`)
    await refreshAfterAction(item.id)
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function install(item: MarketItem) {
  try {
    await post('/api/market/installations', {
      capability_id: item.id, capability_name: item.name, kind: item.type
    })
    ElMessage.success(`已安装「${item.name}」到云端托管`)
    await refreshAfterAction(item.id)
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function uninstall(item: MarketItem) {
  try {
    await ElMessageBox.confirm(`确认卸载「${item.name}」? 卸载后该连接器工具将立即下线。`,
      '卸载确认', { type: 'warning' })
  } catch { return }
  try {
    await del(`/api/market/installations/${item.id}`)
    ElMessage.success(`已卸载「${item.name}」`)
    await refreshAfterAction(item.id)
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function toggleEnabled(item: MarketItem, value: string | number | boolean) {
  const enabled = Boolean(value)
  try {
    await patch(`/api/market/installations/${item.id}`, { enabled })
    ElMessage.success(enabled ? '已启用' : '已停用')
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
  await refreshAfterAction(item.id)
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2>能力市场</h2>
        <div class="sub">浏览市场能力：加入后可将连接器安装到云端托管（由平台网关调用，无需本机进程）</div>
      </div>
    </div>

    <div class="bar">
      <el-input v-model="query.q" placeholder="搜索能力名称 / 描述" clearable style="width: 260px"
                @keyup.enter="search" />
      <el-select v-model="query.type" style="width: 140px" @change="search">
        <el-option v-for="opt in typeOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
      </el-select>
      <el-button type="primary" @click="search">搜索</el-button>
      <el-button @click="resetSearch">重置</el-button>
    </div>

    <el-alert v-if="marketMissing" type="warning" :closable="false" show-icon
              title="能力市场未配置，请联系管理员" style="margin-bottom: 12px" />

    <div v-loading="loading">
      <el-table v-if="!marketMissing" :data="items" stripe>
        <el-table-column label="名称" min-width="180">
          <template #default="{ row }">
            <el-button link type="primary" @click="openDetail(row)">{{ row.name }}</el-button>
            <div class="muted">{{ row.version || '' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ typeLabel(row.type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">{{ row.description || '—' }}</template>
        </el-table-column>
        <el-table-column label="标签" min-width="150">
          <template #default="{ row }">
            <el-tag v-for="t in (row.tags || []).slice(0, 3)" :key="t" size="small" effect="plain"
                    style="margin-right: 4px">{{ t }}</el-tag>
            <span v-if="!(row.tags || []).length" class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="分发" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="distTagType(row.distribution)">{{ distLabel(row.distribution) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="150">
          <template #default="{ row }">
            <el-tag v-if="row.joined" size="small" type="success" effect="light" style="margin-right: 4px">已加入</el-tag>
            <el-tag v-if="row.installed" size="small" type="primary" effect="light">已安装</el-tag>
            <span v-if="!row.joined && !row.installed" class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="290" fixed="right">
          <template #default="{ row }">
            <el-button v-if="!row.joined" size="small" type="primary" plain @click="join(row)">加入</el-button>
            <el-button v-else size="small" plain @click="leave(row)">移出</el-button>
            <el-button v-if="canInstall(row) && !row.installed" size="small" type="primary"
                       @click="install(row)">安装（云端托管）</el-button>
            <el-button v-if="row.installed" size="small" type="danger" plain
                       @click="uninstall(row)">卸载</el-button>
            <el-switch v-if="row.installed" :model-value="enabledOf(row)" inline-prompt
                       active-text="启用" inactive-text="停用"
                       @change="(v: string | number | boolean) => toggleEnabled(row, v)" />
          </template>
        </el-table-column>
      </el-table>

      <div v-if="!loading && (marketMissing || items.length === 0)" style="margin-top: 12px">
        <el-empty v-if="marketMissing" description="能力市场未配置，请联系管理员" />
        <el-empty v-else :description="query.q || query.type ? '无匹配能力' : '暂无能力'" />
      </div>

      <div v-if="!marketMissing && total > 0" class="pager">
        <el-pagination v-model:current-page="query.page" v-model:page-size="query.page_size"
                       :total="total" :page-sizes="[12, 24, 48]" layout="total, sizes, prev, pager, next"
                       @current-change="load" @size-change="search" />
      </div>
    </div>

    <el-drawer v-model="drawer" size="480px" :title="detail?.name || '能力详情'">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <div style="margin-bottom: 10px">
            <el-tag size="small" effect="plain" style="margin-right: 6px">{{ typeLabel(detail.type) }}</el-tag>
            <el-tag size="small" :type="distTagType(detail.distribution)" style="margin-right: 6px">
              {{ distLabel(detail.distribution) }}
            </el-tag>
            <el-tag v-if="detail.version" size="small" effect="plain">v{{ detail.version }}</el-tag>
          </div>
          <el-alert v-if="detail.distribution === 'local'" type="warning" :closable="false" show-icon
                    title="该能力为本地分发：需管理员在「本地安装」配置，不支持云端托管" style="margin-bottom: 10px" />
          <el-alert v-else-if="detail.type === 'mcp' && detail.joined" type="info" :closable="false" show-icon
                    title="安装到云端托管后由平台网关调用，无需本机进程" style="margin-bottom: 10px" />

          <p class="detail-desc">{{ detail.description || '暂无说明' }}</p>
          <div v-if="(detail.tags || []).length" style="margin-bottom: 12px">
            <el-tag v-for="t in detail.tags" :key="t" size="small" effect="plain"
                    style="margin: 0 4px 4px 0">{{ t }}</el-tag>
          </div>

          <template v-if="detail.components && detail.components.length">
            <div class="section-title">组件（{{ detail.components.length }}）</div>
            <el-table :data="detail.components" size="small" border>
              <el-table-column label="名称" prop="name" min-width="120" />
              <el-table-column label="类型" width="90">
                <template #default="{ row }">{{ typeLabel(row.type || '') }}</template>
              </el-table-column>
              <el-table-column label="说明" prop="description" min-width="140" show-overflow-tooltip />
            </el-table>
          </template>

          <div class="drawer-actions">
            <el-button v-if="!detail.joined" type="primary" @click="join(detail)">加入</el-button>
            <el-button v-else @click="leave(detail)">移出</el-button>
            <el-button v-if="canInstall(detail) && !detail.installed" type="primary"
                       @click="install(detail)">安装（云端托管）</el-button>
            <el-button v-if="detail.installed" type="danger" plain @click="uninstall(detail)">卸载</el-button>
            <el-switch v-if="detail.installed" :model-value="enabledOf(detail)" inline-prompt
                       active-text="启用" inactive-text="停用"
                       @change="(v: string | number | boolean) => toggleEnabled(detail!, v)" />
          </div>
        </template>
        <el-empty v-else description="详情加载失败" />
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.bar { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.muted { color: var(--text-3); font-size: 12px; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
.detail-desc { font-size: 13px; line-height: 1.7; white-space: pre-wrap; color: var(--text-2); }
.section-title { font-size: 13px; font-weight: 600; margin: 6px 0 8px; }
.drawer-actions { display: flex; align-items: center; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
</style>
