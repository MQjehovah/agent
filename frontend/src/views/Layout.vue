<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useTheme } from '../theme'
  import { api, clearToken, getRole, hasPerm, setIdentity } from '../api'
  import logoUrl from '../assets/logo.svg'
import {
  Monitor, ChatDotRound, Timer, Coin,
  Odometer, User, Setting, Moon, Sunny, Fold, Expand, SwitchButton,
  Shop, Connection, Files, Collection
} from '@element-plus/icons-vue'

const route = useRoute()
const router = useRouter()
const { theme, toggle } = useTheme()
const collapsed = ref(false)

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
  { path: '/dashboard', title: '工作台', icon: Monitor },
  { path: '/chat', title: '对话', icon: ChatDotRound },
  { path: '/knowledge', title: '知识库', icon: Collection },
  { path: '/market', title: '能力市场', icon: Shop },
  { path: '/connectors', title: '我的连接器', icon: Connection },
  { path: '/scheduler', title: '定时任务', icon: Timer },
  { path: '/memories', title: '记忆管理', icon: Coin }
]

// —— 一级：运维与管理（按细粒度权限展示）——
const allOpsItems: NavItem[] = [
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

onMounted(refreshMe)

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
  </div>
</template>

<style scoped>
.side-foot { display: flex; align-items: center; gap: 6px; }
.side-foot .el-dropdown { flex: 1; min-width: 0; }
.ops-title { margin-top: 14px; }
.foot-user {
  display: flex; align-items: center; gap: 9px;
  padding: 7px 8px 10px; overflow: hidden;
  cursor: pointer; border-radius: 8px;
}
.foot-user:hover { background: var(--bg-hover); }
.user-avatar {
  width: 30px; height: 30px; flex: none; border-radius: 50%;
  background: var(--accent-dim); color: var(--accent);
  display: flex; align-items: center; justify-content: center;
  font-size: 13px; font-weight: 650;
}
.user-name { font-size: 13px; font-weight: 600; color: var(--text); white-space: nowrap; }
.user-role { font-size: 11px; color: var(--text-3); margin-top: 1px; white-space: nowrap; }
</style>
