<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useTheme } from '../theme'
  import { api, clearToken, getRole, hasPerm, setIdentity } from '../api'
  import logoUrl from '../assets/logo.svg'
import CommandPalette from '../components/CommandPalette.vue'
import { dingtalkGroupDisplayName, isDingtalkGroupSession } from '../channel'
import {
  Monitor, ChatDotRound, Timer, Coin,
  Odometer, User, Setting, Moon, Sunny, Fold, Expand, SwitchButton,
  Shop, Files, Collection, Clock, Search, Refresh, CaretRight, FolderOpened
} from '@element-plus/icons-vue'

const route = useRoute()
const router = useRouter()
const { theme, toggle } = useTheme()
const collapsed = ref(false)

/** 命令面板(Ctrl+K)/新建任务(Ctrl+N) */
const paletteVisible = ref(false)
function newTask(): void {
  void router.push({ path: '/chat', query: { new: String(Date.now()) } })
}
function isTypingTarget(el: EventTarget | null): boolean {
  const node = el as HTMLElement | null
  if (!node) return false
  const tag = node.tagName?.toLowerCase()
  return tag === 'input' || tag === 'textarea' || node.isContentEditable === true
}
function onKeydown(e: KeyboardEvent): void {
  if (isTypingTarget(e.target)) return
  if (e.ctrlKey && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    paletteVisible.value = true
    return
  }
  if (e.ctrlKey && !e.shiftKey && e.key.toLowerCase() === 'n') {
    e.preventDefault()
    newTask()
  }
}
function onPaletteNavigate(path: string): void {
  paletteVisible.value = false
  router.push(path)
}
function onPaletteOpenSession(id: string): void {
  paletteVisible.value = false
  void router.push({ path: '/chat', query: { session: id } })
}
function onPaletteNewTask(): void {
  paletteVisible.value = false
  newTask()
}

/** 侧栏会话列表(与对话页同源)，点击经 /chat?session= 打开 */
interface SideSession { id: string; channel: string; at?: string; streaming?: boolean }
const sideSessions = ref<SideSession[]>([])
const sideLoading = ref(false)
const sessionsCollapsed = ref(false)
const currentSessionId = computed(() => String(route.query.session ?? ''))

/** 会话渠道细类(按 id 前缀, 与桌面端一致) */
function chanKind(s: SideSession): 'web' | 'dingtalk' | 'dingtalk_group' | 'other' {
  if (s.id.startsWith('dingtalk_group:')) return 'dingtalk_group'
  if (s.id.startsWith('dingtalk:')) return 'dingtalk'
  if (s.id.startsWith('web:')) return 'web'
  return s.channel === 'web' ? 'web' : s.channel === 'dingtalk' ? 'dingtalk' : 'other'
}
function chanShort(s: SideSession): string {
  const k = chanKind(s)
  return k === 'web' ? 'Web' : k === 'dingtalk' ? '钉钉' : k === 'dingtalk_group' ? '钉钉群' : '其他'
}
/** 会话显示名: 群用群名, 其余去掉 `channel:userid:` 前缀只留 hash(与桌面端一致) */
function sessionLabel(s: SideSession): string {
  if (isDingtalkGroupSession(s.id)) return dingtalkGroupDisplayName(s.id)
  const m = /^[^:]+:\d+:(.+)$/.exec(s.id)
  return m ? m[1] : s.id
}
async function loadSideSessions(): Promise<void> {
  sideLoading.value = true
  try {
    const [hist, live] = await Promise.all([
      api<{ sessions: any[] }>('/api/agent/sessions/history?limit=200').catch(() => ({ sessions: [] })),
      api<{ sessions: any[] }>('/api/sessions').catch(() => ({ sessions: [] }))
    ])
    const map = new Map<string, SideSession>()
    for (const h of hist.sessions ?? []) {
      if (h.id) map.set(h.id, { id: h.id, channel: h.channel || 'other', at: h.last_accessed || h.first_accessed })
    }
    for (const l of live.sessions ?? []) {
      if (l.id) {
        const prev = map.get(l.id)
        map.set(l.id, { id: l.id, channel: prev?.channel || 'web', at: prev?.at || l.created_at, streaming: !!l.is_streaming })
      }
    }
    sideSessions.value = Array.from(map.values()).sort((a, b) => String(b.at || '').localeCompare(String(a.at || '')))
  } finally {
    sideLoading.value = false
  }
}
function openSideSession(id: string): void {
  void router.push({ path: '/chat', query: { session: id } })
}
function shortTime(t?: string): string {
  if (!t) return ''
  const d = new Date(t)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit' })
}

const role = ref(getRole())
const me = ref<{ name: string; role: string; department?: string }>({ name: '', role: '' })
const permsVersion = ref(0)

interface NavItem {
  path: string
  title: string
  icon: any
  perm?: string
}

// —— 一级：个人空间（所有登录用户，平铺）——
const personalItems: NavItem[] = [
  { path: '/chat', title: '对话', icon: ChatDotRound },
  { path: '/sessions', title: '会话历史', icon: Clock },
  { path: '/knowledge', title: '知识库', icon: Collection },
  { path: '/market', title: '能力市场', icon: Shop },
  { path: '/scheduler', title: '定时任务', icon: Timer },
  { path: '/memories', title: '记忆管理', icon: Coin }
]

// —— 一级：运维与管理（按细粒度权限展示）——
const allOpsItems: NavItem[] = [
  { path: '/dashboard', title: '工作台', icon: Monitor },
  { path: '/monitor', title: '运行监控', icon: Odometer, perm: 'admin.monitor' },
  { path: '/admin/local-mcp', title: '本地安装', icon: Files, perm: 'admin.mcp_local' },
  { path: '/admin', title: '用户与权限', icon: User,
    perm: 'admin.users' }
]
const opsItems = computed(() => {
  void permsVersion.value
  return allOpsItems.filter((i) => !i.perm || hasPerm(i.perm)
    || (i.path === '/admin' && (hasPerm('admin.roles') || hasPerm('admin.departments')))
    || (i.path === '/monitor' && (hasPerm('admin.logs') || hasPerm('admin.scheduler') || hasPerm('admin.memories'))))
})
const showOps = computed(() => opsItems.value.length > 0)

const allNavPaths = computed(() => [...personalItems, ...opsItems.value].map((i) => i.path))

function itemActive(item: NavItem): boolean {
  // 取最长前缀匹配: 避免 /admin/local-mcp 同时点亮 /admin
  const matched = allNavPaths.value.filter((p) => route.path === p || route.path.startsWith(p + '/'))
  if (!matched.length) return false
  return matched.reduce((a, b) => (b.length > a.length ? b : a)) === item.path
}

function go(item: NavItem) {
  if (collapsed.value) collapsed.value = false
  router.push(item.path)
}

const currentTitle = computed(() => (route.meta.title as string) ?? '')

const roleLabel = computed(() => {
  const r = me.value.role || role.value
  const base = r === 'admin' ? '管理员' : (r === 'user' ? '普通用户' : '成员')
  return me.value.department ? `${base} · ${me.value.department}` : base
})

async function refreshMe() {
  try {
    const d = await api<{ id?: number; name: string; display_name?: string; role: string;
      department?: string; permissions?: string[]; data_scope?: string }>('/api/auth/me')
    me.value = { name: d.display_name || d.name, role: d.role, department: d.department }
    setIdentity({ role: d.role, permissions: d.permissions, data_scope: d.data_scope, department: d.department })
    role.value = d.role
    permsVersion.value++
  } catch {
    /* 忽略 */
  }
}

let sideTimer: number | undefined
onMounted(() => {
  void refreshMe()
  void loadSideSessions()
  sideTimer = window.setInterval(() => void loadSideSessions(), 15000)
  window.addEventListener('keydown', onKeydown)
})
onBeforeUnmount(() => {
  if (sideTimer) window.clearInterval(sideTimer)
  window.removeEventListener('keydown', onKeydown)
})

async function logout() {
  try { await api('/api/auth/logout', { method: 'POST' }) } catch { /* 忽略 */ }
  clearToken()
  router.push('/login')
}

function onUserCommand(cmd: string | number | object) {
  if (cmd === 'logout') void logout()
}
</script>

<template>
  <div class="layout" :class="{ collapsed }">
    <aside class="sider">
      <div class="brand"><img class="logo" :src="logoUrl" alt="Rosiwit" /><span class="side-label">零号员工</span></div>
      <div class="side-actions">
        <button class="side-action" @click="newTask">
          <el-icon :size="15"><SwitchButton /></el-icon>
          <span class="side-label">新建任务</span>
          <span class="side-kbd">Ctrl+N</span>
        </button>
        <button class="side-action" @click="paletteVisible = true">
          <el-icon :size="15"><Search /></el-icon>
          <span class="side-label">搜索</span>
          <span class="side-kbd">Ctrl+K</span>
        </button>
      </div>
      <nav>
        <div class="side-group-title">个人空间</div>
        <a v-for="item in personalItems" :key="item.path" href="#"
           :class="{ 'router-link-active': itemActive(item) }"
           @click.prevent="go(item)">
          <el-icon :size="15"><component :is="item.icon" /></el-icon>
          <span class="side-label">{{ item.title }}</span>
        </a>

        <template v-if="showOps">
          <div class="side-group-title ops-title">运维与管理</div>
          <a v-for="item in opsItems" :key="item.path" href="#"
             :class="{ 'router-link-active': itemActive(item) }"
             @click.prevent="go(item)">
            <el-icon :size="15"><component :is="item.icon" /></el-icon>
            <span class="side-label">{{ item.title }}</span>
          </a>
        </template>
      </nav>

      <div class="side-section">
        <div class="side-section-title collapsible" @click="sessionsCollapsed = !sessionsCollapsed">
          <el-icon :size="14" class="side-caret" :class="{ open: !sessionsCollapsed }"><CaretRight /></el-icon>
          会话
          <el-icon :size="15" class="side-section-action" :class="{ 'is-loading': sideLoading }" title="刷新" @click.stop="loadSideSessions"><Refresh /></el-icon>
        </div>
        <div v-show="!sessionsCollapsed" class="side-sessions">
          <div
            v-for="s in sideSessions"
            :key="s.id"
            class="side-session"
            :class="{ active: s.id === currentSessionId }"
            :title="s.id"
            @click="openSideSession(s.id)"
          >
            <el-icon :size="13" class="side-session-icon"><FolderOpened /></el-icon>
            <span class="side-session-name">{{ sessionLabel(s) }}</span>
            <span class="side-session-chan" :class="'chan-' + chanKind(s)">{{ chanShort(s) }}</span>
            <span v-if="s.streaming" class="side-session-tag" title="运行中">运行中</span>
            <span class="side-session-time">{{ shortTime(s.at) }}</span>
          </div>
          <div v-if="!sideSessions.length" class="side-empty">暂无会话</div>
        </div>
      </div>

      <div class="side-foot">
        <el-dropdown trigger="click" placement="top-start" @command="onUserCommand">
          <div class="foot-user" title="点击菜单">
            <div class="user-avatar">{{ (me.name || '?').slice(0, 1).toUpperCase() }}</div>
            <div class="side-label">
              <div class="user-name">{{ me.name || '未登录' }}</div>
              <div class="user-role">{{ roleLabel }}</div>
            </div>
          </div>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item disabled>{{ me.name || '未登录' }} · {{ roleLabel }}</el-dropdown-item>
              <el-dropdown-item command="logout" divided>
                <el-icon :size="14" style="margin-right: 6px"><SwitchButton /></el-icon>退出登录
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <button class="icon-btn" title="设置" @click="router.push('/settings')">
          <el-icon :size="15"><Setting /></el-icon>
        </button>
      </div>
    </aside>

    <main class="main">
      <div class="topbar">
        <button class="icon-btn" @click="collapsed = !collapsed">
          <el-icon :size="16"><component :is="collapsed ? Expand : Fold" /></el-icon>
        </button>
        <span class="title">{{ currentTitle }}</span>
        <span class="spacer" />
        <button class="icon-btn" :title="theme === 'dark' ? '切换浅色' : '切换深色'" @click="toggle">
          <el-icon :size="16"><component :is="theme === 'dark' ? Sunny : Moon" /></el-icon>
        </button>
      </div>
      <div class="content">
        <router-view v-slot="{ Component }">
          <keep-alive include="ChatView">
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </div>
    </main>
    <CommandPalette
      v-model="paletteVisible"
      @navigate="onPaletteNavigate"
      @open-session="onPaletteOpenSession"
      @new-task="onPaletteNewTask"
    />
  </div>
</template>

<style scoped>
.side-foot { display: flex; align-items: center; gap: 6px; }
.side-foot .el-dropdown { flex: 1; min-width: 0; }
.ops-title { margin-top: 14px; }
.side-actions { display: flex; flex-direction: column; gap: 2px; margin-bottom: 10px; }
.side-action {
  display: flex; align-items: center; gap: 10px; width: 100%;
  border: none; background: transparent; color: var(--text-2);
  padding: 8.5px 10px; border-radius: 8px; cursor: pointer;
  font-size: 13.5px; white-space: nowrap;
}
.side-action:hover { background: var(--bg-hover); color: var(--text); }
.side-kbd { margin-left: auto; font-size: 11px; color: var(--text-3); font-family: Consolas, monospace; }
.layout.collapsed .side-action { justify-content: center; padding: 9px 0; }
.layout.collapsed .side-kbd { display: none; }

/* 侧栏会话列表(对齐桌面端: 主侧栏内置) */
.sider nav { flex: none; overflow: visible; }
.side-section { flex: 1; min-height: 0; display: flex; flex-direction: column; margin-top: 12px; }
.side-section-title {
  display: flex; align-items: center; gap: 6px;
  font-size: 13px; letter-spacing: 0.02em;
  color: var(--text-3); padding: 0 10px 6px;
}
.side-section-action { margin-left: auto; cursor: pointer; }
.side-section-action:hover { color: var(--text); }
.side-sessions { flex: 1; overflow-y: auto; min-height: 0; }
.side-session {
  display: flex; align-items: center; gap: 7px;
  padding: 6px 8px; border-radius: 8px; cursor: pointer;
  color: var(--text-2); font-size: 13.5px; margin-bottom: 1px;
}
.side-session:hover { background: var(--bg-hover); }
.side-session.active { background: var(--el-fill-color-darker); color: var(--text); }
.side-session-icon { flex: none; color: var(--text-3); }
.side-session-name {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-family: Consolas, monospace;
}
.side-session-time { flex: none; font-size: 10.5px; color: var(--text-3); }
.side-session-tag { flex: none; font-size: 10px; color: var(--el-color-warning); }
.side-empty { padding: 6px 8px; font-size: 12px; color: var(--text-3); }
.layout.collapsed .side-section { display: none; }
.side-section-title.collapsible { cursor: pointer; user-select: none; }
.side-section-title.collapsible:hover { color: var(--text-2); }
.side-caret { flex: none; transition: transform 0.15s; }
.side-caret.open { transform: rotate(90deg); }
.side-session-chan {
  flex: none; padding: 0 5px; border-radius: 8px; font-size: 10px; line-height: 15px;
  border: 1px solid transparent; white-space: nowrap;
}
.side-session-chan.chan-web { color: var(--el-color-primary); background: var(--el-color-primary-light-9); border-color: var(--el-color-primary-light-7); }
.side-session-chan.chan-dingtalk { color: var(--el-color-success); background: var(--el-color-success-light-9); border-color: var(--el-color-success-light-7); }
.side-session-chan.chan-dingtalk_group { color: var(--el-color-warning); background: var(--el-color-warning-light-9); border-color: var(--el-color-warning-light-7); }
.side-session-chan.chan-other { color: var(--el-text-color-secondary); background: var(--el-fill-color-light); border-color: var(--el-border-color-lighter); }
.foot-user {
  display: flex; align-items: center; gap: 9px;
  width: 100%; min-width: 0;
  padding: 7px 8px 10px; overflow: hidden;
  cursor: pointer; border-radius: 8px;
}
.foot-user:hover { background: var(--bg-hover); }
.foot-user .side-label { flex: 1; min-width: 0; overflow: hidden; }
.user-avatar {
  width: 30px; height: 30px; flex: none; border-radius: 50%;
  background: var(--accent-dim); color: var(--accent);
  display: flex; align-items: center; justify-content: center;
  font-size: 13px; font-weight: 650;
}
.user-name { font-size: 13px; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.user-role { font-size: 11px; color: var(--text-3); margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
</style>
