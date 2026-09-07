<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useTheme } from '../theme'
import { api, clearToken, getRole, setRole } from '../api'
import {
  Monitor, ChatDotRound, Clock, Timer, Coin, Document,
  Connection, User, Setting, Moon, Sunny, Fold, Expand, SwitchButton,
  Odometer, ArrowDown, ArrowUp
} from '@element-plus/icons-vue'

const route = useRoute()
const router = useRouter()
const { theme, toggle } = useTheme()
const collapsed = ref(false)

const role = ref(getRole())
const admin = computed(() => role.value === 'admin')
const me = ref<{ name: string; role: string }>({ name: '', role: '' })

interface NavItem {
  path: string
  title: string
  icon: any
  query?: Record<string, string>
  active?: (r: ReturnType<typeof useRoute>) => boolean
}

// —— 一级：个人空间（所有登录用户）——
const personalItems: NavItem[] = [
  { path: '/dashboard', title: '工作台', icon: Monitor },
  { path: '/chat', title: '对话', icon: ChatDotRound },
  {
    path: '/scheduler', title: '定时任务', icon: Timer,
    active: r => r.path.startsWith('/scheduler') && r.query.scope !== 'all'
  },
  {
    path: '/memories', title: '记忆管理', icon: Coin,
    active: r => r.path.startsWith('/memories') && r.query.view !== 'all'
  }
]

// —— 一级：运行监控（仅管理员，可展开二级）——
const opsItems: NavItem[] = [
  { path: '/monitor', title: '运行监控', icon: Odometer },
  { path: '/sessions', title: '会话管理', icon: Clock },
  {
    path: '/scheduler', title: '定时任务', icon: Timer, query: { scope: 'all' },
    active: r => r.path.startsWith('/scheduler') && r.query.scope === 'all'
  },
  {
    path: '/memories', title: '记忆管理', icon: Coin, query: { view: 'all' },
    active: r => r.path.startsWith('/memories') && r.query.view === 'all'
  },
  { path: '/logs', title: '日志', icon: Document },
  { path: '/webhook', title: 'Webhook', icon: Connection },
  { path: '/admin', title: '用户管理', icon: User },
  { path: '/settings', title: '设置', icon: Setting }
]

const opsActive = computed(() => opsItems.some(itemActive))
const opsOpen = ref(false)
const opsExpanded = computed({
  get: () => opsOpen.value || opsActive.value,
  set: (v: boolean) => { opsOpen.value = v }
})

function itemActive(item: NavItem): boolean {
  if (item.active) return item.active(route)
  const q = item.query ?? {}
  for (const [k, v] of Object.entries(q)) {
    if (String(route.query[k] ?? '') !== v) return false
  }
  return route.path.startsWith(item.path)
}

function go(item: NavItem) {
  if (collapsed.value) collapsed.value = false
  router.push({ path: item.path, query: item.query })
}

function toggleOps() {
  if (collapsed.value) {
    collapsed.value = false
    opsOpen.value = true
    return
  }
  opsExpanded.value = !opsExpanded.value
}

const currentTitle = computed(() => (route.meta.title as string) ?? '')

const roleLabel = computed(() => {
  const r = me.value.role || role.value
  if (r === 'admin') return '管理员'
  if (r === 'user') return '普通用户'
  return '成员'
})

async function refreshMe() {
  try {
    const d = await api<{ id?: number; name: string; role: string }>('/api/auth/me')
    me.value = { name: d.name, role: d.role }
    if (d.role) { role.value = d.role; setRole(d.role) }
  } catch {
    /* 忽略 */
  }
}

onMounted(refreshMe)

async function logout() {
  try { await api('/api/auth/logout', { method: 'POST' }) } catch { /* 忽略 */ }
  clearToken()
  router.push('/login')
}
</script>

<template>
  <div class="layout" :class="{ collapsed }">
    <aside class="sider">
      <div class="brand"><span class="logo">零</span><span class="side-label">零号员工</span></div>
      <nav>
        <div class="side-group-title">个人空间</div>
        <a v-for="item in personalItems" :key="item.path" href="#"
           :class="{ 'router-link-active': itemActive(item) }"
           @click.prevent="go(item)">
          <el-icon :size="15"><component :is="item.icon" /></el-icon>
          <span class="side-label">{{ item.title }}</span>
        </a>

        <template v-if="admin">
          <div class="nav-sub-head" :class="{ open: opsExpanded, 'router-link-active': opsActive }" @click="toggleOps">
            <el-icon :size="15"><Odometer /></el-icon>
            <span class="side-label">运行监控</span>
            <el-icon v-if="!collapsed" class="caret side-label" :size="12">
              <component :is="opsExpanded ? ArrowUp : ArrowDown" />
            </el-icon>
          </div>
          <template v-if="opsExpanded && !collapsed">
            <a v-for="item in opsItems" :key="item.path + JSON.stringify(item.query ?? {})" href="#"
               class="nav-sub-item" :class="{ 'router-link-active': itemActive(item) }"
               @click.prevent="go(item)">
              <el-icon :size="15"><component :is="item.icon" /></el-icon>
              <span class="side-label">{{ item.title }}</span>
            </a>
          </template>
        </template>
      </nav>

      <div class="side-foot">
        <div class="foot-user">
          <div class="user-avatar">{{ (me.name || '?').slice(0, 1).toUpperCase() }}</div>
          <div class="side-label">
            <div class="user-name">{{ me.name || '未登录' }}</div>
            <div class="user-role">{{ roleLabel }}</div>
          </div>
        </div>
        <div class="foot-row" @click="logout">
          <el-icon :size="14"><SwitchButton /></el-icon>
          <span class="side-foot-name">退出登录</span>
        </div>
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
  </div>
</template>

<style scoped>
.nav-sub-head {
  display: flex; align-items: center; gap: 10px;
  padding: 8.5px 10px; border-radius: 8px;
  color: var(--text-2); font-size: 13.5px;
  white-space: nowrap; cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.nav-sub-head:hover { background: var(--bg-hover); color: var(--text); }
.nav-sub-head.open { color: var(--text); }
.nav-sub-head.router-link-active { color: var(--accent); }
.nav-sub-head .caret { margin-left: auto; flex: none; }
.layout.collapsed .nav-sub-head { justify-content: center; padding: 9px 0; }
.layout.collapsed .nav-sub-head .caret { display: none; }

nav a.nav-sub-item { padding-left: 28px; }

.foot-user {
  display: flex; align-items: center; gap: 9px;
  padding: 7px 8px 10px; overflow: hidden;
}
.user-avatar {
  width: 30px; height: 30px; flex: none; border-radius: 50%;
  background: var(--accent-dim); color: var(--accent);
  display: flex; align-items: center; justify-content: center;
  font-size: 13px; font-weight: 650;
}
.user-name { font-size: 13px; font-weight: 600; color: var(--text); white-space: nowrap; }
.user-role { font-size: 11px; color: var(--text-3); margin-top: 1px; white-space: nowrap; }
</style>
