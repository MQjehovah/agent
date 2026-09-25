<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  CaretRight,
  ChatDotRound,
  Clock,
  Coin,
  Collection,
  Delete,
  FolderOpened,
  Loading,
  MagicStick,
  MoreFilled,
  Refresh,
  Search,
  Setting,
  SwitchButton,
  Timer,
  Top,
  UserFilled
} from '@element-plus/icons-vue'
import {
  canContinueInDashboard,
  channelKindFromId,
  channelLabel,
  useSessionsStore,
  type SessionListItem
} from '../stores/sessions'
import { useChatStore } from '../stores/chat'
import { useSettingsStore } from '../stores/settings'
import { agentApi } from '../api/agent'
import CommandPalette from '../components/CommandPalette.vue'
import logoUrl from '../assets/logo.svg'

const route = useRoute()
const router = useRouter()
const sessionsStore = useSessionsStore()
const chat = useChatStore()
const settings = useSettingsStore()

/** 新建任务:清空当前会话并回到对话页(ZCode 的 Ctrl+N 语义) */
function newTask() {
  chat.newSession()
  void router.push('/chat')
}

/** 新建临时会话(仅本地):退出/启动时自动删除, 侧栏有「临时」徽标 */
async function newEphemeralTask(): Promise<void> {
  if (chat.streaming) return
  try {
    await chat.startLocalSession(undefined, undefined, { ephemeral: true })
    // 未设置默认工作区时会弹目录选择, 取消则中止
    if (!chat.sessionId) return
    await sessionsStore.refresh(true)
    await router.push('/chat')
    ElMessage.success('已创建临时会话(退出后自动删除)')
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

/** 侧栏「新建任务」菜单:普通 / 临时 */
function onNewCommand(command: string): void {
  if (command === 'ephemeral') void newEphemeralTask()
  else newTask()
}

/** 快速提问窗提交:新建会话发送;成功才请主进程关窗,失败/流式中回执错误并保留输入 */
async function handleQuickPrompt(text: string): Promise<void> {
  const message = text.trim()
  const reject = (reason: string): void => {
    void window.desktop.invoke('quick:reject', { message: reason }).catch(() => {})
  }
  if (!message) {
    reject('请输入内容')
    return
  }
  if (chat.streaming) {
    ElMessage.warning('正在生成回答,请稍后再试')
    reject('正在生成回答,请稍后再试')
    return
  }
  void router.push('/chat')
  if (chat.sessionMode === 'local') {
    // 本地模式沿既有新建路径(未设置默认工作区时会弹目录选择)
    await chat.startLocalSession()
    if (!chat.sessionId) {
      reject('已取消:未选择工作区')
      return
    }
    // 新会话刚落盘:刷新列表,通知标题等才能取到会话名
    await sessionsStore.refresh(true)
  } else {
    chat.newSession()
  }
  // 在线路径强制新建会话:避免启动期自动打开最近会话等并发写入旧 sessionId
  await chat.send(message, { forceNewSession: chat.sessionMode === 'agent' })
  if (chat.error) {
    reject(chat.error)
    return
  }
  void window.desktop.invoke('quick:close').catch(() => {})
}

/** 通知点击打开会话:优先查已加载列表,未收录的在线会话回退拉取历史 */
async function handleOpenSession(sessionId: string): Promise<void> {
  if (!sessionId || chat.streaming) return
  const hit = sessionsStore.sessions.find((s) => s.id === sessionId)
  if (hit) {
    await openSession(hit)
    return
  }
  await sessionsStore.refresh(true)
  const again = sessionsStore.sessions.find((s) => s.id === sessionId)
  if (again) {
    await openSession(again)
    return
  }
  if (sessionId.startsWith('web:')) {
    try {
      const res = await agentApi.sessionMessages(sessionId)
      chat.loadHistory(sessionId, res.messages ?? [])
      void router.push('/chat')
    } catch (err) {
      ElMessage.error(`加载消息失败:${(err as Error).message}`)
    }
  }
}

/** 会话按在线(云端 agent)/ 离线(本地模式)分组(主列表不含已归档) */
const sessionGroups = computed(() => [
  {
    key: 'online',
    label: '在线会话',
    items: sessionsStore.activeSessions.filter((s) => s.mode !== 'local')
  },
  {
    key: 'offline',
    label: '离线会话',
    items: sessionsStore.activeSessions.filter((s) => s.mode === 'local')
  }
])

/** 侧栏渠道细类(优先服务端 channel_kind, 缺失按 ID 前缀兜底) */
function sideChannelKind(s: SessionListItem): string {
  return s.channelKind || channelKindFromId(s.id)
}
/** 侧栏渠道短标签(在线会话标注 web 私聊/群) */
function sideChannelShort(s: SessionListItem): string {
  switch (sideChannelKind(s)) {
    case 'web':
      return 'Web'
    case 'dingtalk':
      return '钉钉'
    case 'dingtalk_group':
      return '钉钉群'
    default:
      return '其他'
  }
}
/** 侧栏渠道 class(用于配色); 同时标记能否在 dashboard 续聊 */
function sideChannelClass(s: SessionListItem): string {
  return 'chan-' + sideChannelKind(s)
}
function sideCanContinue(s: SessionListItem): boolean {
  return canContinueInDashboard(s)
}

/** 已归档分组默认折叠; 仅在有内容时显示 */
const archivedOpen = ref(false)
/** 会话列表折叠(默认展开) */
const sessionsCollapsed = ref(false)

/** 打开会话:本地会话切到本地模式,agent 会话走 HTTP 历史 */
async function openSession(s: SessionListItem) {
  if (chat.streaming) return
  try {
    if (s.mode === 'local') {
      await chat.loadLocalMessages(s.id, { workspace: s.workspace, ephemeral: s.ephemeral })
    } else {
      const res = await agentApi.sessionMessages(s.id)
      chat.loadHistory(s.id, res.messages ?? [])
    }
    void router.push('/chat')
  } catch (err) {
    ElMessage.error(`加载消息失败:${(err as Error).message}`)
  }
}

/** 会话操作菜单: 置顶 / 重命名 / 归档 / 导出 / 删除(归档与导出对临时会话禁用) */
async function onSessionCommand(cmd: string, s: SessionListItem) {
  if (cmd === 'delete') {
    await removeSession(s)
    return
  }
  if (cmd === 'pin') {
    try {
      await sessionsStore.setPinned(s.id, !s.pinned, s.mode)
      ElMessage.success(s.pinned ? '已取消置顶' : '已置顶')
    } catch (err) {
      ElMessage.error(`操作失败:${(err as Error).message}`)
    }
    return
  }
  if (cmd === 'rename') {
    try {
      const { value } = await ElMessageBox.prompt('输入新的会话名称(留空恢复默认)', '重命名会话', {
        inputValue: s.title,
        inputValidator: (v: string) => (v ?? '').length <= 60 || '名称不超过 60 字',
        confirmButtonText: '保存',
        cancelButtonText: '取消'
      })
      await sessionsStore.rename(s.id, (value ?? '').trim(), s.mode)
      ElMessage.success('已重命名')
    } catch (err) {
      if (err !== 'cancel') ElMessage.error(`重命名失败:${(err as Error).message}`)
    }
    return
  }
  if (cmd === 'archive' || cmd === 'unarchive') {
    const archived = cmd === 'archive'
    try {
      await sessionsStore.setArchived(s.mode, s.id, archived)
      ElMessage.success(archived ? '已归档,可在列表底部「已归档」中恢复' : '已取消归档')
    } catch (err) {
      ElMessage.error(`操作失败:${(err as Error).message}`)
    }
    return
  }
  if (cmd === 'exportMd' || cmd === 'exportJson') {
    try {
      const res = await sessionsStore.exportSession(s.mode, s.id, cmd === 'exportMd' ? 'md' : 'json', s.title)
      if (res.canceled) return
      ElMessage.success(`已导出:${res.path ?? ''}`)
    } catch (err) {
      ElMessage.error(`导出失败:${(err as Error).message}`)
    }
  }
}

async function removeSession(s: SessionListItem) {
  try {
    await ElMessageBox.confirm(`确认删除会话 ${s.title}？\n删除后不再显示（消息数据保留，可由管理员恢复）`, '删除会话', { type: 'warning' })
  } catch {
    return
  }
  try {
    if (chat.sessionId === s.id) chat.newSession()
    await sessionsStore.remove(s.id, s.mode)
    ElMessage.success('已删除')
  } catch (err) {
    ElMessage.error(`删除失败:${(err as Error).message}`)
  }
}

/** 焦点在输入控件内时不拦截全局快捷键,避免打字误触发 */
function isTypingTarget(el: EventTarget | null): boolean {
  const node = el as HTMLElement | null
  if (!node) return false
  const tag = node.tagName?.toLowerCase()
  return tag === 'input' || tag === 'textarea' || node.isContentEditable === true
}

function onKeydown(e: KeyboardEvent) {
  if (isTypingTarget(e.target)) return
  if (e.ctrlKey && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    paletteVisible.value = true
    return
  }
  // Ctrl+Shift+N:新建临时会话(仅本地); 需先于 Ctrl+N 分支判断
  if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'n') {
    e.preventDefault()
    void newEphemeralTask()
    return
  }
  if (e.ctrlKey && e.key.toLowerCase() === 'n') {
    e.preventDefault()
    newTask()
  }
}

let refreshTimer: number | undefined
/** 桌面壳事件订阅的取消函数(托盘/快速提问窗/通知) */
let offQuickPrompt: (() => void) | undefined
let offOpenSession: (() => void) | undefined
let offNewSession: (() => void) | undefined

/** 启动时默认打开最近更新的会话(只做一次; 用户主动新建任务后不再干扰) */
let autoOpened = false

async function autoOpenLatestSession(): Promise<void> {
  if (autoOpened) return
  autoOpened = true
  if (chat.streaming || chat.sessionId || chat.messages.length) return
  // 已归档会话不参与「最近会话」自动打开(与主列表口径一致)
  const latest = [...sessionsStore.activeSessions].sort((a, b) => b.updatedAt - a.updatedAt)[0]
  if (!latest) return
  await openSession(latest)
}

onMounted(async () => {
  window.addEventListener('keydown', onKeydown)
  // 桌面壳事件:快速提问文本、通知点击打开会话、托盘新建会话
  offQuickPrompt = window.desktop.onQuickPrompt((payload) => void handleQuickPrompt(payload.text))
  offOpenSession = window.desktop.onOpenSession((payload) => void handleOpenSession(payload.sessionId))
  offNewSession = window.desktop.onNewSession(() => newTask())
  await sessionsStore.refresh(true)
  await autoOpenLatestSession()
  // 会话列表与最近会话就绪后再通知主进程补发排队事件:
  // 否则排队中的 quick-prompt 可能撞上 autoOpenLatestSession 的 loadHistory
  void window.desktop.invoke('desktop:ready').catch(() => {})
  // 会话列表兜底静默刷新
  refreshTimer = window.setInterval(() => void sessionsStore.refresh(true), 15000)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  offQuickPrompt?.()
  offOpenSession?.()
  offNewSession?.()
  if (refreshTimer) window.clearInterval(refreshTimer)
})

function formatTime(t: string | number): string {
  const d = new Date(t)
  if (Number.isNaN(d.getTime())) return String(t)
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  return sameDay ? d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
}

const navMain = [
  { path: '/chat', title: '对话', icon: ChatDotRound },
  { path: '/sessions', title: '会话历史', icon: Clock },
  { path: '/knowledge', title: '知识库', icon: Collection },
  { path: '/market', title: '能力市场', icon: MagicStick },
  { path: '/scheduler', title: '定时任务', icon: Timer },
  { path: '/memories', title: '记忆管理', icon: Coin }
]

const userInitial = (): string => {
  const name = settings.user?.name ?? ''
  return name ? name.slice(0, 1).toUpperCase() : '?'
}

const paletteVisible = ref(false)

/** 命令面板跳转:关闭面板后交给路由 */
function onPaletteNavigate(path: string) {
  paletteVisible.value = false
  void router.push(path)
}

/** 命令面板打开会话:复用布局既有 openSession(本地/远程自动分支) */
function onPaletteOpenSession(item: SessionListItem) {
  paletteVisible.value = false
  void openSession(item)
}

/** 命令面板新建任务:复用布局既有 newTask(清空当前会话) */
function onPaletteNewTask() {
  paletteVisible.value = false
  newTask()
}

function onUserCommand(command: string) {
  if (command === 'profile') void router.push('/profile')
  else if (command === 'logout') void doLogout()
}

async function doLogout() {
  await settings.logout()
  ElMessage.success('已退出登录')
  chat.newSession()
  void router.push('/chat')
}
</script>

<template>
  <div class="workbench">
    <!-- 顶部拖拽区(无边框窗口):品牌 logo + 当前页面标题 -->
    <div class="drag-strip">
      <img class="drag-logo" :src="logoUrl" alt="" />
      <span class="drag-title">{{ route.meta?.title || '员工 AI 工作台' }}</span>
    </div>

    <aside class="sidebar">
      <div class="side-top">
        <!-- 新建任务菜单: 普通会话 / 临时会话(仅本地, 退出即删) -->
        <el-dropdown trigger="click" class="side-new-dropdown" @command="onNewCommand">
          <button class="side-item">
            <el-icon :size="15"><SwitchButton /></el-icon>
            <span class="side-label">新建任务</span>
            <span class="side-kbd">Ctrl+N</span>
          </button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="normal">新建任务</el-dropdown-item>
              <el-dropdown-item command="ephemeral">
                新建临时会话（退出即删）
                <span class="dd-kbd">Ctrl+Shift+N</span>
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <button class="side-item" title="搜索" @click="paletteVisible = true">
          <el-icon :size="15"><Search /></el-icon>
          <span class="side-label">搜索</span>
          <span class="side-kbd">Ctrl+K</span>
        </button>

        <div class="side-group">
          <router-link
            v-for="item in navMain"
            :key="item.path"
            :to="item.path"
            class="side-item"
            :class="{ active: route.path.startsWith(item.path) }"
          >
            <el-icon :size="15"><component :is="item.icon" /></el-icon>
            <span class="side-label">{{ item.title }}</span>
          </router-link>
        </div>

        <div class="side-section">
          <span class="side-section-title collapsible" @click="sessionsCollapsed = !sessionsCollapsed">
            <el-icon :size="11" class="side-caret" :class="{ open: !sessionsCollapsed }"><CaretRight /></el-icon>
            会话
            <el-icon v-if="sessionsStore.loading" class="is-loading spin"><Loading /></el-icon>
            <el-icon v-else class="side-section-action" title="刷新" @click.stop="sessionsStore.refresh()"><Refresh /></el-icon>
          </span>
          <div v-show="!sessionsCollapsed" class="side-sessions">
            <div v-for="group in sessionGroups" :key="group.key" class="side-group">
              <div v-if="group.items.length" class="side-group-label">
                {{ group.label }}
                <span class="side-group-count">{{ group.items.length }}</span>
              </div>
              <div
                v-for="s in group.items"
                :key="s.id"
                class="side-session"
                :class="{ active: s.id === chat.sessionId }"
                :title="s.mode === 'local' ? `本地 · ${s.workspace ?? ''}` : `在线 · ${channelLabel(sideChannelKind(s))}${sideCanContinue(s) ? '' : ' · 只读，请在对应渠道继续'} · ${s.id}`"
                @click="openSession(s)"
              >
                <el-icon :size="13" class="side-session-icon"><FolderOpened /></el-icon>
                <span class="side-session-name">{{ s.title }}</span>
                <span
                  v-if="s.mode !== 'local'"
                  class="side-session-chan"
                  :class="sideChannelClass(s)"
                  :title="channelLabel(sideChannelKind(s))"
                >{{ sideChannelShort(s) }}</span>
                <span
                  v-if="s.ephemeral"
                  class="side-session-tag"
                  title="退出后自动删除，不落历史"
                >临时</span>
                <el-icon v-if="s.pinned" :size="11" class="side-session-pin" title="已置顶"><Top /></el-icon>
                <span class="side-session-time">{{ formatTime(s.createdAt) }}</span>
                <el-dropdown
                  trigger="click"
                  class="side-session-menu"
                  @command="(cmd: string) => onSessionCommand(cmd, s)"
                  @click.stop
                >
                  <el-icon :size="13" class="side-session-more" @click.stop><MoreFilled /></el-icon>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item v-if="s.mode !== 'local'" command="pin">
                        {{ s.pinned ? '取消置顶' : '置顶' }}
                      </el-dropdown-item>
                      <el-dropdown-item command="rename">重命名</el-dropdown-item>
                      <el-dropdown-item v-if="s.ephemeral" disabled>临时会话不可归档/导出</el-dropdown-item>
                      <template v-else>
                        <el-dropdown-item command="archive">归档</el-dropdown-item>
                        <el-dropdown-item command="exportMd">导出 Markdown</el-dropdown-item>
                        <el-dropdown-item command="exportJson">导出 JSON</el-dropdown-item>
                      </template>
                      <el-dropdown-item command="delete" divided>删除</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </div>
            </div>

            <!-- 已归档分组: 列表底部折叠展示, 仅有内容时出现 -->
            <div v-if="sessionsStore.archivedSessions.length" class="side-group">
              <div class="side-group-label side-archived-toggle" @click="archivedOpen = !archivedOpen">
                <el-icon :size="11" class="side-archived-caret" :class="{ open: archivedOpen }"><CaretRight /></el-icon>
                已归档
                <span class="side-group-count">{{ sessionsStore.archivedSessions.length }}</span>
              </div>
              <template v-if="archivedOpen">
                <div
                  v-for="s in sessionsStore.archivedSessions"
                  :key="'arch-' + s.id"
                  class="side-session archived"
                  :class="{ active: s.id === chat.sessionId }"
                  :title="`已归档 · ${s.title}`"
                  @click="openSession(s)"
                >
                  <el-icon :size="13" class="side-session-icon"><FolderOpened /></el-icon>
                  <span class="side-session-name">{{ s.title }}</span>
                  <el-dropdown
                    trigger="click"
                    class="side-session-menu"
                    @command="(cmd: string) => onSessionCommand(cmd, s)"
                    @click.stop
                  >
                    <el-icon :size="13" class="side-session-more" @click.stop><MoreFilled /></el-icon>
                    <template #dropdown>
                      <el-dropdown-menu>
                        <el-dropdown-item command="unarchive">取消归档</el-dropdown-item>
                        <el-dropdown-item command="exportMd">导出 Markdown</el-dropdown-item>
                        <el-dropdown-item command="exportJson">导出 JSON</el-dropdown-item>
                        <el-dropdown-item command="delete" divided>删除</el-dropdown-item>
                      </el-dropdown-menu>
                    </template>
                  </el-dropdown>
                </div>
              </template>
            </div>

            <div v-if="sessionsStore.loaded && sessionsStore.activeSessions.length === 0 && !sessionsStore.archivedSessions.length" class="side-empty">暂无会话</div>
            <div v-if="!sessionsStore.loaded" class="side-empty">未连接 agent</div>
          </div>
        </div>
      </div>

      <div class="side-footer">
        <el-dropdown trigger="click" class="user-menu" @command="onUserCommand">
          <div class="user-trigger">
            <span class="avatar" :class="{ ghost: !settings.user }">{{ userInitial() }}</span>
            <span class="side-footer-name">{{ settings.user ? settings.user.name : '未登录' }}</span>
          </div>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="profile">
                <el-icon :size="14" class="dd-icon"><UserFilled /></el-icon>个人中心
              </el-dropdown-item>
              <el-dropdown-item command="logout" divided>
                <el-icon :size="14" class="dd-icon"><SwitchButton /></el-icon>退出登录
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <button class="side-icon-btn" title="设置" @click="router.push('/settings')">
          <el-icon :size="15"><Setting /></el-icon>
        </button>
      </div>
    </aside>

    <main class="content">
      <router-view />
    </main>

    <!-- 命令面板(Ctrl+K),导航/会话跳转由布局接管 -->
    <CommandPalette
      v-model="paletteVisible"
      @navigate="onPaletteNavigate"
      @open-session="onPaletteOpenSession"
      @new-task="onPaletteNewTask"
    />
  </div>
</template>
