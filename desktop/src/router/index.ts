import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'
import WorkbenchLayout from '../layouts/WorkbenchLayout.vue'

const routes: Array<RouteRecordRaw> = [
  // 快速提问小窗(无边框置顶)#/quick:独立于工作台布局与登录门
  { path: '/quick', name: 'quick', component: () => import('../views/QuickPromptView.vue'), meta: { title: '快速提问' } },
  {
    path: '/',
    component: WorkbenchLayout,
    redirect: '/chat',
    children: [
      { path: 'chat', name: 'chat', component: () => import('../views/ChatView.vue'), meta: { title: '对话' } },
      { path: 'sessions', name: 'sessions', component: () => import('../views/SessionsView.vue'), meta: { title: '会话历史' } },
      { path: 'knowledge', name: 'knowledge', component: () => import('../views/KnowledgeView.vue'), meta: { title: '知识库' } },
      { path: 'market', name: 'market', component: () => import('../views/MarketView.vue'), meta: { title: '能力市场' } },
      { path: 'profile', name: 'profile', component: () => import('../views/ProfileView.vue'), meta: { title: '个人中心' } },
      // 用量已并入个人中心,保留旧路径重定向避免历史链接失效
      { path: 'usage', redirect: '/profile' },
      { path: 'settings', name: 'settings', component: () => import('../views/SettingsView.vue'), meta: { title: '设置' } }
    ]
  },
  { path: '/:pathMatch(.*)*', redirect: '/chat' }
]

const router = createRouter({
  history: createWebHashHistory(),
  routes
})

export default router
