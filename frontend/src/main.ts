import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import App from './App.vue'
import './style.css'
import { hasPerm } from './api'

import LoginView from './views/LoginView.vue'
import DashboardView from './views/DashboardView.vue'
import WebhookView from './views/WebhookView.vue'
import ChatView from './views/ChatView.vue'
import SessionsView from './views/SessionsView.vue'
import KanbanView from './views/KanbanView.vue'
import SchedulerView from './views/SchedulerView.vue'
import MemoriesView from './views/MemoriesView.vue'
import MarketView from './views/MarketView.vue'
import ConnectorsView from './views/ConnectorsView.vue'
import LocalMcpView from './views/LocalMcpView.vue'
import AdminView from './views/AdminView.vue'
import MonitorView from './views/MonitorView.vue'
import LogsView from './views/LogsView.vue'
import SettingsView from './views/SettingsView.vue'
import KnowledgeView from './views/KnowledgeView.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', component: LoginView, meta: { public: true, title: '登录' } },
    {
      path: '/',
      component: () => import('./views/Layout.vue'),
      redirect: '/dashboard',
      children: [
        { path: 'dashboard', component: DashboardView, meta: { title: '工作台' } },
        { path: 'chat', component: ChatView, meta: { title: '对话' } },
        { path: 'knowledge', component: KnowledgeView, meta: { title: '知识库' } },
        { path: 'scheduler', component: SchedulerView, meta: { title: '定时任务' } },
        { path: 'memories', component: MemoriesView, meta: { title: '记忆管理' } },
        { path: 'market', component: MarketView, meta: { title: '能力市场' } },
        { path: 'connectors', component: ConnectorsView, meta: { title: '我的连接器' } },
        { path: 'settings', component: SettingsView, meta: { title: '设置' } },
        { path: 'sessions', component: SessionsView, meta: { title: '会话历史' } },
        { path: 'kanban', component: KanbanView, meta: { title: '任务看板', perm: 'admin.monitor' } },
        { path: 'monitor', component: MonitorView, meta: { title: '运行监控',
          permAny: ['admin.monitor', 'admin.logs', 'admin.scheduler', 'admin.memories'] } },
        { path: 'logs', component: LogsView, meta: { title: '日志', perm: 'admin.logs' } },
        { path: 'webhook', component: WebhookView, meta: { title: 'Webhook', perm: 'admin.monitor' } },
        { path: 'admin', component: AdminView, meta: { title: '用户与权限',
          permAny: ['admin.users', 'admin.roles', 'admin.departments'] } },
        { path: 'admin/local-mcp', component: LocalMcpView,
          meta: { title: '本地安装', perm: 'admin.mcp_local' } }
      ]
    },
    { path: '/:pathMatch(.*)*', redirect: '/chat' }
  ]
})

router.beforeEach((to) => {
  document.title = (to.meta.title ? to.meta.title + ' · ' : '') + '零号员工'
  if (!to.meta.public && !localStorage.getItem('agent_jwt')) return '/login'
  // 权限路由守卫: 细粒度 Web 权限(后端仍强制鉴权, 此处仅导航收敛)
  const permAny = to.meta.permAny as string[] | undefined
  if (permAny && !permAny.some((p) => hasPerm(p))) return '/dashboard'
  if (to.meta.perm && !hasPerm(to.meta.perm as string)) return '/dashboard'
  // 「运行监控」组内页的全量 query: 无对应权限一律回个人空间
  if (!hasPerm('admin.scheduler') && to.path === '/scheduler' && to.query.scope === 'all') return '/dashboard'
  if (!hasPerm('admin.memories') && to.path === '/memories' && to.query.view === 'all') return '/dashboard'
})

const app = createApp(App)
app.use(router)
app.use(ElementPlus)
app.mount('#app')
