import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import App from './App.vue'
import './style.css'

import LoginView from './views/LoginView.vue'
import DashboardView from './views/DashboardView.vue'
import WebhookView from './views/WebhookView.vue'
import ChatView from './views/ChatView.vue'
import SessionsView from './views/SessionsView.vue'
import KanbanView from './views/KanbanView.vue'
import SchedulerView from './views/SchedulerView.vue'
import MemoriesView from './views/MemoriesView.vue'
import AdminView from './views/AdminView.vue'
import LogsView from './views/LogsView.vue'
import SettingsView from './views/SettingsView.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', component: LoginView, meta: { public: true, title: '登录' } },
    {
      path: '/',
      component: () => import('./views/Layout.vue'),
      redirect: '/dashboard',
      children: [
        { path: 'dashboard', component: DashboardView, meta: { title: '总览' } },
        { path: 'chat', component: ChatView, meta: { title: '对话' } },
        { path: 'sessions', component: SessionsView, meta: { title: '会话' } },
        { path: 'kanban', component: KanbanView, meta: { title: '看板' } },
        { path: 'scheduler', component: SchedulerView, meta: { title: '定时任务' } },
        { path: 'memories', component: MemoriesView, meta: { title: '记忆' } },
        { path: 'logs', component: LogsView, meta: { title: '日志' } },
        { path: 'webhook', component: WebhookView, meta: { title: 'Webhook' } },
        { path: 'admin', component: AdminView, meta: { title: '用户管理' } },
        { path: 'settings', component: SettingsView, meta: { title: '设置' } }
      ]
    },
    { path: '/:pathMatch(.*)*', redirect: '/chat' }
  ]
})

router.beforeEach((to) => {
  document.title = (to.meta.title ? to.meta.title + ' · ' : '') + '零号员工'
  if (!to.meta.public && !localStorage.getItem('agent_jwt')) return '/login'
})

const app = createApp(App)
app.use(router)
app.use(ElementPlus)
app.mount('#app')
