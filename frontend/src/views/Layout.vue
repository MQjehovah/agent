<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useTheme } from '../theme'
import { api, clearToken, isAdmin } from '../api'
import {
  Monitor, ChatDotRound, Clock, DataBoard, Timer, Coin, Document, DataLine,
  Connection, User, Setting, Moon, Sunny, Fold, Expand, SwitchButton,
  Odometer
} from '@element-plus/icons-vue'

const route = useRoute()
const router = useRouter()
const { theme, toggle } = useTheme()
const collapsed = ref(false)
const admin = isAdmin()

const groups = [
  {
    title: '工作台',
    items: [
      { path: '/dashboard', title: '总览', icon: Monitor },
      { path: '/chat', title: '对话', icon: ChatDotRound },
      { path: '/sessions', title: '会话', icon: Clock },
      { path: '/usage', title: '用量', icon: DataLine },
      { path: '/kanban', title: '看板', icon: DataBoard },
      { path: '/scheduler', title: '定时任务', icon: Timer },
      { path: '/memories', title: '记忆', icon: Coin }
    ]
  },
  {
    title: '运维与管理',
    items: [
      { path: '/monitor', title: '运行监控', icon: Odometer, adminOnly: true },
      { path: '/logs', title: '日志', icon: Document },
      { path: '/webhook', title: 'Webhook', icon: Connection },
      { path: '/admin', title: '用户管理', icon: User, adminOnly: true },
      { path: '/settings', title: '设置', icon: Setting }
    ]
  }
]

const visibleGroups = computed(() =>
  groups.map(g => ({
    ...g,
    items: g.items.filter(i => !i.adminOnly || admin)
  })).filter(g => g.items.length > 0)
)

const currentTitle = computed(() => (route.meta.title as string) ?? '')

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
        <template v-for="g in visibleGroups" :key="g.title">
          <div class="side-group-title">{{ g.title }}</div>
          <a v-for="item in g.items" :key="item.path" :href="'#' + item.path"
             :class="{ 'router-link-active': route.path.startsWith(item.path) }">
            <el-icon :size="15"><component :is="item.icon" /></el-icon>
            <span class="side-label">{{ item.title }}</span>
          </a>
        </template>
      </nav>
      <div class="side-foot">
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
