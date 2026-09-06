<script setup lang="ts">
import { useRouter } from 'vue-router'
import { ChatDotRound, Clock, DataBoard, Timer, Coin, Document, User, Setting } from '@element-plus/icons-vue'

const router = useRouter()
const nav = [
  { path: '/chat', title: '对话', icon: ChatDotRound },
  { path: '/sessions', title: '会话', icon: Clock },
  { path: '/kanban', title: '看板', icon: DataBoard },
  { path: '/scheduler', title: '定时任务', icon: Timer },
  { path: '/memories', title: '记忆', icon: Coin },
  { path: '/logs', title: '日志', icon: Document },
  { path: '/admin', title: '用户管理', icon: User },
  { path: '/settings', title: '设置', icon: Setting }
]
function logout() {
  localStorage.removeItem('agent_jwt')
  router.push('/login')
}
</script>

<template>
  <div class="layout">
    <aside class="sider">
      <div class="brand"><span class="logo">零</span><span>零号员工</span></div>
      <nav>
        <a v-for="n in nav" :key="n.path" :href="'#' + n.path">
          <el-icon :size="15"><component :is="n.icon" /></el-icon>{{ n.title }}
        </a>
      </nav>
      <div class="foot">
        <div>{{ '已登录' }}</div>
        <el-link type="primary" :underline="false" @click="logout">退出登录</el-link>
      </div>
    </aside>
    <main class="main">
      <router-view />
    </main>
  </div>
</template>
