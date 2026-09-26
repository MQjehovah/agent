<script setup lang="ts">
defineOptions({ name: 'ChatView' })
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, post, streamChat } from '../api'
import { channelMeta, dingtalkGroupDisplayName, isDingtalkGroupSession } from '../channel'
import MarkdownIt from 'markdown-it'
import { CaretRight, Warning, ArrowRight, Plus, Microphone, MagicStick, Check, Picture } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import AttachmentImage from '../components/AttachmentImage.vue'

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

/** 推荐问句(点一下填进输入框) */
const suggestions = [
  '帮我总结今天的会议要点',
  '查询设备在线状态与告警',
  '起草一份本周工作周报',
  '从知识库检索报销制度'
]

/** 权限模式(在线): default 每次询问 / smart 必要时询问 / auto 完全访问 */
const PERM_MODES: Record<'default' | 'smart' | 'auto', { label: string; hint: string }> = {
  default: { label: '默认(每次询问)', hint: '写操作需确认' },
  smart: { label: '智能(必要时询问)', hint: '仅高危操作需确认' },
  auto: { label: '完全访问', hint: '不询问，直接执行' }
}
const permMode = ref<'default' | 'smart' | 'auto'>('default')
const permLabel = computed(() => PERM_MODES[permMode.value].label)
const onlineModel = ref('')
const plusOpen = ref(false)

/** 待发送图片（先上传到附件库，拿到引用；消息只带引用，不带字节） */
const pendingAttachments = ref<{ ref: string; name: string; url: string }[]>([])
const filePicker = ref<HTMLInputElement | null>(null)
function pickImage() {
  filePicker.value?.click()
}
function fileToDataUrl(f: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result))
    r.onerror = reject
    r.readAsDataURL(f)
  })
}
async function readImageFiles(files: FileList | File[] | null | undefined) {
  if (!files) return
  for (const f of Array.from(files)) {
    if (!f.type.startsWith('image/')) continue
    try {
      const dataUrl = await fileToDataUrl(f)
      const meta = await post<{ ref: string; name: string; url?: string }>('/api/attachments', {
        name: f.name || 'image.png',
        data_url: dataUrl
      })
      pendingAttachments.value.push({ ref: meta.ref, name: meta.name || f.name, url: meta.url || '' })
    } catch (e) {
      ElMessage.error(`图片上传失败：${(e as Error).message}`)
    }
  }
}
function onPickImage(e: Event) {
  const el = e.target as HTMLInputElement
  void readImageFiles(el.files)
  el.value = ''
}
function onPasteImage(e: ClipboardEvent) {
  const items = e.clipboardData?.items
  if (!items) return
  const files: File[] = []
  for (const it of Array.from(items)) {
    if (it.type.startsWith('image/')) {
      const f = it.getAsFile()
      if (f) files.push(f)
    }
  }
  if (files.length) {
    e.preventDefault()
    void readImageFiles(files)
  }
}
function removeAttachment(i: number) {
  pendingAttachments.value.splice(i, 1)
}

/** 入口: company=零号员工(企业单例) / personal=我的员工助手(个人实例) */
const entryScope = ref<'company' | 'personal'>('company')
watch(entryScope, () => {
  if (sessionId.value) newSession()
})
const entryLabel = computed(() => (entryScope.value === 'personal' ? '员工助手' : '零号员工'))
function onEntry(c: 'company' | 'personal') {
  entryScope.value = c
}

async function loadOnlineModel(): Promise<void> {
  try {
    const s = await api<{ model?: string }>('/api/agent/status')
    onlineModel.value = String(s?.model ?? '').trim()
  } catch {
    /* 忽略 */
  }
}

interface TextBlock { kind: 'text'; content: string; sealed?: boolean; html: string }
interface ToolBlock { kind: 'tool'; name: string }
interface AgentBlock {
  kind: 'agent'
  name: string
  running: boolean
  collapsed: boolean
  toolCount: number
  blocks: MsgBlock[]
}
type MsgBlock = TextBlock | ToolBlock | AgentBlock

interface Msg {
  role: 'user' | 'assistant'
  content: string
  reasoning: string
  blocks: MsgBlock[]
  toolCount: number
  error: string
  images?: { ref: string; name: string; url?: string }[]
}
interface SessionRow {
  id: string
  channel: string
  created_at?: string
  last_accessed?: string
  message_count?: number
  is_streaming?: boolean
}

const messages = ref<Msg[]>([])
const input = ref('')
const streaming = ref(false)
const procTip = ref('')
const ask = ref<any>(null)
const askSel = ref('')
const askText = ref('')
const sessionId = ref('')
const sessions = ref<SessionRow[]>([])
const currentSession = ref<SessionRow | null>(null)
const scrollRef = ref<HTMLElement | null>(null)
let abort: AbortController | null = null

const readonly = computed(() => {
  const ch = currentSession.value?.channel
  return !!ch && ch !== 'web'
})

function render(text: string) {
  return md.render(text ?? '')
}

// —— markdown 渲染时机 ——
// 流式期间正文以纯文本即时累积显示(零解析、顺滑)；整段流结束(或历史加载)后
// 才渲染一次 markdown 存 html。避免每 token 全量 v-html 重跑 markdown-it 卡顿。
function renderBlockMarkdown(b: TextBlock) {
  b.html = render(b.content)
}
function flushMarkdown(blocks: MsgBlock[]) {
  for (const b of blocks) {
    if (b.kind === 'text') renderBlockMarkdown(b)
    else if (b.kind === 'agent') flushMarkdown(b.blocks)
  }
}

function scrollBottom() {
  void nextTick(() => {
    const el = scrollRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function shortTime(t?: string): string {
  if (!t) return ''
  const d = new Date(t)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function displayId(id: string): string {
  const m = /^[^:]+:\d+:/i.exec(id)
  return m ? id.slice(m[0].length) : id
}

function displayTitle(s: SessionRow): string {
  if (isDingtalkGroupSession(s.id)) return dingtalkGroupDisplayName(s.id)
  return displayId(s.id)
}

function emptyAssistant(): Msg {
  return { role: 'assistant', content: '', reasoning: '', blocks: [], toolCount: 0, error: '' }
}

function textOf(m: Msg): string {
  return m.blocks
    .filter((b): b is TextBlock => b.kind === 'text')
    .map(b => b.content)
    .join('')
}

/** 顶层/子代理内 追加正文 token：命中尾部未密封 text 块则续写，否则新增 text 块(保持输出顺序) */
function appendText(blocks: MsgBlock[], token: string) {
  const last = blocks[blocks.length - 1]
  if (last && last.kind === 'text' && !last.sealed) last.content += token
  else blocks.push({ kind: 'text', content: token, html: '' })
}

/** 密封尾部 text 块：工具/子代理插入点之前的正文不再续写(呈现换行)，并立即渲染其 markdown */
function sealTailText(blocks: MsgBlock[]) {
  const last = blocks[blocks.length - 1]
  if (last && last.kind === 'text') {
    last.sealed = true
    renderBlockMarkdown(last)
  }
}

/** 找到当前运行中的子代理(最近的 running agent 块)，用于接收 subagent_token/tool 事件 */
function runningAgent(m: Msg): AgentBlock | null {
  for (let i = m.blocks.length - 1; i >= 0; i--) {
    const b = m.blocks[i]
    if (b.kind === 'agent' && b.running) return b
  }
  return null
}

/** 移除最近一个 name 匹配的 tool 块(运行中工具完成即消失)，返回是否命中 */
function finishTool(blocks: MsgBlock[], name: string): boolean {
  for (let i = blocks.length - 1; i >= 0; i--) {
    const b = blocks[i]
    if (b.kind === 'tool' && (!name || b.name === name)) {
      blocks.splice(i, 1)
      return true
    }
  }
  return false
}

async function loadSessions() {
  try {
    const [hist, live] = await Promise.all([
      api<{ sessions: any[] }>('/api/agent/sessions/history?limit=200').catch(() => ({ sessions: [] })),
      api<{ sessions: any[] }>('/api/sessions').catch(() => ({ sessions: [] }))
    ])
    const map = new Map<string, SessionRow>()
    for (const h of hist.sessions ?? []) {
      if (!h.id) continue
      map.set(h.id, {
        id: h.id,
        channel: h.channel || 'other',
        created_at: h.first_accessed,
        last_accessed: h.last_accessed,
        message_count: h.messages ?? 0,
        is_streaming: false
      })
    }
    for (const l of live.sessions ?? []) {
      if (!l.id) continue
      const prev = map.get(l.id)
      map.set(l.id, {
        id: l.id,
        channel: prev?.channel || 'web',
        created_at: prev?.created_at || l.created_at,
        last_accessed: prev?.last_accessed || l.created_at,
        message_count: l.message_count ?? prev?.message_count ?? 0,
        is_streaming: !!l.is_streaming
      })
    }
    sessions.value = Array.from(map.values()).sort((a, b) =>
      (b.last_accessed || b.created_at || '').localeCompare(a.last_accessed || a.created_at || ''))
  } catch { /* 静默 */ }
}

async function fetchMessages(id: string, channel: string): Promise<Array<{ role: string; content: string }>> {
  // 钉钉等外部渠道无内存 ChatSession，直接读 DB 历史（只读）；
  // Web 会话优先读内存快照，落库后/重启后回退 DB。
  if (channel !== 'web') {
    const d = await api<{ messages: Array<{ role: string; content: string }> }>(
      `/api/agent/sessions/messages?session_id=${encodeURIComponent(id)}`)
    return d.messages ?? []
  }
  try {
    const d = await api<{ messages: Array<{ role: string; content: string }> }>(
      `/api/sessions/${encodeURIComponent(id)}/messages`)
    return d.messages ?? []
  } catch {
    const d = await api<{ messages: Array<{ role: string; content: string }> }>(
      `/api/agent/sessions/messages?session_id=${encodeURIComponent(id)}`)
    return d.messages ?? []
  }
}

async function openSession(row: SessionRow) {
  if (streaming.value) return
  try {
    const msgs = await fetchMessages(row.id, row.channel)
    sessionId.value = row.id
    currentSession.value = row
    procTip.value = ''
    messages.value = msgs.map(m => {
      const c: any = m.content
      const isList = Array.isArray(c)
      const text = isList
        ? c.map((p: any) => (p && p.type === 'text' ? p.text : '')).join('')
        : String(c ?? '')
      const imgs = isList
        ? c.filter((p: any) => p && (p.type === 'image_ref' || p.type === 'image_url'))
            .map((p: any) => {
              const raw = String(p.ref || (p.image_url && p.image_url.url) || '')
              const isData = raw.startsWith('data:')
              return { ref: isData ? '' : raw, name: String(p.name || ''), url: isData ? raw : undefined }
            })
            .filter((x: any) => x.ref || x.url)
        : []
      if (m.role === 'user') {
        return {
          role: 'user' as const, content: text, reasoning: '', blocks: [], toolCount: 0, error: '',
          images: imgs.length ? imgs : undefined
        }
      }
      const a = emptyAssistant()
      if (text) a.blocks.push({ kind: 'text', content: text, html: '' })
      return a
    })
    for (const mm of messages.value) flushMarkdown(mm.blocks)
    scrollBottom()
  } catch (e) {
    console.error(e)
  }
}

async function removeSession(row: SessionRow) {
  try { await api(`/api/sessions/${encodeURIComponent(row.id)}`, { method: 'DELETE' }) } catch { return }
  if (sessionId.value === row.id) { sessionId.value = ''; currentSession.value = null; messages.value = [] }
  await loadSessions()
}

function newSession() {
  if (streaming.value) return
  sessionId.value = ''
  currentSession.value = null
  messages.value = []
  procTip.value = ''
}

function send() {
  const text = input.value.trim()
  const imgs = pendingAttachments.value.slice()
  if ((!text && imgs.length === 0) || streaming.value || readonly.value) return
  input.value = ''
  pendingAttachments.value = []
  const shown = text || (imgs.length ? `[图片 ×${imgs.length}]` : '')
  messages.value.push({
    role: 'user',
    content: shown,
    reasoning: '',
    blocks: [],
    toolCount: 0,
    error: '',
    images: imgs.map((a) => ({ ref: a.ref, name: a.name, url: a.url || undefined }))
  })
  const reply: Msg = emptyAssistant()
  messages.value.push(reply)
  streaming.value = true
  procTip.value = '正在处理…'
  scrollBottom()

  abort = new AbortController()
  streamChat(
    {
      message: text,
      session_id: sessionId.value || undefined,
      permission_mode: permMode.value,
      scope: entryScope.value,
      attachments: imgs.length ? imgs.map((a) => ({ ref: a.ref, name: a.name })) : undefined
    },
    (ev) => {
      const data = (ev.data ?? {}) as Record<string, any>
      switch (ev.type) {
        case 'token':
          appendText(reply.blocks, ev.content ?? '')
          procTip.value = ''
          break
        case 'reasoning':
          reply.reasoning += ev.content ?? ''
          if (!textOf(reply)) procTip.value = '思考中…'
          break
        case 'round_start':
          procTip.value = `第 ${data.iteration ?? ''} 轮`
          break
        case 'ask':
          ask.value = ev.data ?? null
          if (ask.value) { askSel.value = ask.value.default ?? ''; askText.value = '' }
          procTip.value = '需要你确认/回答…'
          break
        case 'tool_start':
          sealTailText(reply.blocks)
          reply.blocks.push({ kind: 'tool', name: String(data.name || 'tool') })
          procTip.value = `正在调用工具：${String(data.name || 'tool')}`
          break
        case 'tool_result': {
          const name = String(data.name || 'tool')
          if (finishTool(reply.blocks, name)) reply.toolCount++
          procTip.value = '工具执行完成，继续处理…'
          break
        }
        case 'subagent_start': {
          const name = String(data.agent_name || 'subagent')
          sealTailText(reply.blocks)
          reply.blocks.push({ kind: 'agent', name, running: true, collapsed: false, toolCount: 0, blocks: [] })
          procTip.value = `已派生子代理「${name}」执行`
          break
        }
        case 'subagent_token': {
          const act = runningAgent(reply)
          if (act) appendText(act.blocks, ev.content ?? data.content ?? '')
          procTip.value = '子代理执行中…'
          break
        }
        case 'subagent_tool_start': {
          const act = runningAgent(reply)
          if (act) {
            sealTailText(act.blocks)
            act.blocks.push({ kind: 'tool', name: String(data.name || 'tool') })
            procTip.value = `子代理正在调用工具：${String(data.name || 'tool')}`
          }
          break
        }
        case 'subagent_tool_result': {
          const act = runningAgent(reply)
          if (act) {
            const name = String(data.name || 'tool')
            if (finishTool(act.blocks, name)) act.toolCount++
          }
          break
        }
        case 'subagent_result': {
          const act = runningAgent(reply)
          if (act) {
            act.running = false
            act.collapsed = true
          }
          procTip.value = '子代理已结束，汇总结果中…'
          break
        }
        case 'done':
          // 正文 token 已实时入 blocks；若流内无正文(如 ask/纯工具兜底)，用 done 内容补齐
          if (ev.content && reply.blocks.length === 0) reply.blocks.push({ kind: 'text', content: ev.content, html: '' })
          flushMarkdown(reply.blocks)
          for (let i = reply.blocks.length - 1; i >= 0; i--) {
            const b = reply.blocks[i]
            if (b.kind === 'agent' && b.running) { b.running = false; b.collapsed = true }
          }
          ask.value = null
          procTip.value = ''
          break
        case 'error': reply.error = ev.content ?? '未知错误'; ask.value = null; procTip.value = ''; break
      }
      scrollBottom()
    },
    abort.signal
  )
    .catch(err => {
      if ((err as Error).name !== 'AbortError') reply.error = (err as Error).message
    })
    .finally(() => {
      streaming.value = false
      procTip.value = ''
      abort = null
      flushMarkdown(reply.blocks)
      void loadSessions()
      scrollBottom()
    })
}

function stop() { abort?.abort() }

function toggleAgent(m: Msg, b: AgentBlock) {
  b.collapsed = !b.collapsed
}

async function submitAsk() {
  if (!ask.value) return
  const ans = (ask.value.options && ask.value.options.length) ? askSel.value : askText.value
  try {
    await post('/api/chat/answer', { ask_id: ask.value.ask_id, answer: ans })
    ask.value = null
    askSel.value = ''
    askText.value = ''
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function cancelAsk() {
  if (!ask.value) return
  try {
    await post('/api/chat/answer', { ask_id: ask.value.ask_id, answer: ask.value.default ?? '' })
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
  ask.value = null
}

/** 从「会话历史」跳转:按 ?session=<id> 打开指定会话 */
const route = useRoute()
const router = useRouter()
async function openFromQuery(): Promise<void> {
  const sid = String(route.query.session ?? '').trim()
  if (!sid || streaming.value) return
  if (sessionId.value === sid) return
  const row = sessions.value.find((s) => s.id === sid) ?? {
    id: sid,
    channel: sid.startsWith('web:') ? 'web' : sid.startsWith('dingtalk') ? 'dingtalk' : 'other',
    message_count: 0
  }
  await openSession(row)
}

let pollTimer: number | undefined
onMounted(async () => {
  void loadOnlineModel()
  await loadSessions()
  await openFromQuery()
  pollTimer = window.setInterval(() => void loadSessions(), 15000)
})
watch(
  () => route.query.session,
  () => void openFromQuery()
)
// 侧栏「新建任务」:跳转到 /chat?new=<ts> 时清空当前会话
watch(
  () => route.query.new,
  (v) => {
    if (v) newSession()
  }
)
onBeforeUnmount(() => {
  abort?.abort()
  if (pollTimer) window.clearInterval(pollTimer)
})
</script>

<template>
  <div class="chat">
    <div class="chat-main">
      <header class="chat-head">
        <span class="chat-title">{{ sessionId || '新对话' }}</span>
        <el-radio-group v-model="entryScope" size="small">
          <el-radio-button value="company">零号员工</el-radio-button>
          <el-radio-button value="personal">员工助手</el-radio-button>
        </el-radio-group>
      </header>
      <div ref="scrollRef" class="chat-scroll">
        <div v-if="messages.length === 0" class="chat-empty">
          <h1>你好，我是{{ entryLabel }}</h1>
          <p>{{ entryScope === 'personal' ? '你的专属工作助手' : '你的全能 AI 助手' }}</p>
          <div class="suggestions">
            <button v-for="s in suggestions" :key="s" class="suggestion" @click="input = s">{{ s }}</button>
          </div>
        </div>

        <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
          <div class="bubble" style="min-width: 30%">
            <!-- 用户消息：图片 + 纯文本 -->
            <template v-if="m.role === 'user'">
              <div v-if="m.images && m.images.length" class="msg-images">
                <AttachmentImage
                  v-for="(im, ii) in m.images"
                  :key="ii"
                  :ref-id="im.ref"
                  :url="im.url"
                  :alt="im.name"
                />
              </div>
              <div v-if="m.content">{{ m.content }}</div>
            </template>

            <!-- 助手消息：按输出顺序的时间线流 -->
            <template v-else>
              <details v-if="m.reasoning" class="reasoning">
                <summary class="reasoning-summary">思考过程</summary>
                <div class="reasoning-text">{{ m.reasoning }}</div>
              </details>

              <div v-for="(b, bi) in m.blocks" :key="bi" class="flow-block">
                <!-- 正文：优先显示节流渲染的 html，首帧未完成时回退纯文本(不阻塞) -->
                <div v-if="b.kind === 'text' && b.content" class="md">
                  <div v-if="!b.html" class="md-plain">{{ b.content }}</div>
                  <div v-else v-html="b.html" />
                </div>
                <!-- 运行中的工具：仅运行时显示 -->
                <div v-else-if="b.kind === 'tool'" class="tool-chip">
                  <span class="chip-running">●</span>
                  <span class="tool-name mono">{{ b.name }}</span>
                  <span style="color: var(--text-3); font-size: 11px">执行中…</span>
                </div>
                <!-- 子代理子块：可折叠 -->
                <div v-else-if="b.kind === 'agent'" class="agent-card" :class="{ running: b.running }">
                  <div class="agent-head" @click="toggleAgent(m, b)">
                    <el-icon :size="12" class="chev" :class="{ open: !b.collapsed }"><ArrowRight /></el-icon>
                    <span class="agent-name mono">{{ b.name }}</span>
                    <el-tag size="small" :type="b.running ? 'warning' : 'success'">{{ b.running ? '执行中' : '完成' }}</el-tag>
                    <span v-if="b.toolCount > 0" class="agent-tools">调用 {{ b.toolCount }} 次工具</span>
                  </div>
                  <div v-if="b.running || !b.collapsed" class="agent-body">
                    <div v-for="(ab, abi) in b.blocks" :key="abi" class="flow-block">
                      <div v-if="ab.kind === 'text' && ab.content" class="agent-stream">
                        <div v-if="!ab.html" class="md-plain">{{ ab.content }}</div>
                        <div v-else v-html="ab.html" />
                      </div>
                      <div v-else-if="ab.kind === 'tool'" class="tool-chip">
                        <span class="chip-running">●</span>
                        <span class="tool-name mono">{{ ab.name }}</span>
                        <span style="color: var(--text-3); font-size: 11px">执行中…</span>
                      </div>
                    </div>
                    <div v-if="b.blocks.length === 0 && b.running" style="color: var(--text-3); font-size: 12px; padding: 2px 0">子代理思考中…</div>
                  </div>
                </div>
              </div>

              <!-- 已完成工具计数汇总 -->
              <div v-if="m.toolCount > 0" class="tools-sum">已调用 {{ m.toolCount }} 次工具</div>

              <div v-if="streaming && i === messages.length - 1 && (procTip || !textOf(m))" class="proc-hint">
                <span class="chip-running">●</span>
                <template v-if="procTip">{{ procTip }}</template>
                <template v-else>正在处理…</template>
              </div>
              <div v-else-if="streaming && i === messages.length - 1" class="caret-line">
                <span class="caret">▍</span>
              </div>
              <el-alert v-if="m.error" :title="m.error" type="error" :closable="false" class="err" />
            </template>
          </div>
        </div>
      </div>

      <footer class="composer-wrap">
        <div class="suggest-row">
          <button v-for="s in suggestions" :key="s" class="suggest-chip" :disabled="streaming" @click="input = s">{{ s }}</button>
        </div>
        <div class="composer" :class="{ readonly }">
          <div v-if="ask" class="ask-bar">
            <div class="ask-q"><el-tag size="small" type="warning" effect="plain">需你确认</el-tag> {{ ask.question }}</div>
            <div v-if="ask.options && ask.options.length" style="margin-top: 6px">
              <el-radio-group v-model="askSel">
                <el-radio v-for="op in ask.options" :key="op" :value="op">{{ op }}</el-radio>
              </el-radio-group>
            </div>
            <el-input v-else v-model="askText" size="small" placeholder="输入回答后回车…" @keyup.enter="submitAsk" style="margin-top:6px" />
            <div style="margin-top: 8px; display: flex; gap: 8px">
              <el-button type="primary" size="small" @click="submitAsk">提交</el-button>
              <el-button size="small" @click="cancelAsk">默认/取消</el-button>
            </div>
          </div>
          <div v-if="readonly" class="composer-readonly">
            <el-icon :size="14"><Warning /></el-icon>
            <span>该会话来自「{{ channelMeta(currentSession?.channel || '', currentSession?.id || '').label }}」渠道，仅支持查看历史，请在对应渠道继续对话。</span>
          </div>
          <div v-if="pendingAttachments.length" class="pending-images">
            <span v-for="(a, i) in pendingAttachments" :key="a.ref" class="pending-chip" :title="a.name">
              <AttachmentImage :ref-id="a.ref" :url="a.url" :alt="a.name" class="chip-thumb" />
              <span class="chip-name">{{ a.name }}</span>
              <button class="thumb-x" title="移除" @click="removeAttachment(i)">×</button>
            </span>
          </div>
          <el-input
            v-model="input"
            type="textarea"
            :autosize="{ minRows: 3, maxRows: 12 }"
            :disabled="readonly"
            :placeholder="readonly ? '该会话只读，不可发送消息' : '今天帮你做些什么？（可粘贴/上传图片）'"
            resize="none"
            class="composer-input"
            @keydown.enter.exact.prevent="send"
            @paste="onPasteImage"
          />
          <div class="composer-bar">
            <div class="composer-left">
              <input
                ref="filePicker"
                type="file"
                accept="image/*"
                multiple
                style="display: none"
                @change="onPickImage"
              />
              <button class="plus-btn" :disabled="streaming || readonly" title="添加图片" @click="pickImage">
                <el-icon :size="15"><Picture /></el-icon>
              </button>
              <el-popover v-model:visible="plusOpen" trigger="click" placement="top-start" :width="300" :show-arrow="false" popper-class="plus-popper">
                <template #reference>
                  <button class="plus-btn" :disabled="streaming || readonly" title="添加">
                    <el-icon :size="15"><Plus /></el-icon>
                  </button>
                </template>
                <div class="plus-menu">
                  <button class="plus-item" @click="plusOpen = false; router.push('/market')">
                    <el-icon :size="14"><MagicStick /></el-icon>
                    <span class="plus-label">管理专家 / 技能 / 连接器</span>
                  </button>
                  <p class="plus-empty">人设 / 语音等请使用桌面端</p>
                </div>
              </el-popover>
              <span class="composer-hint">Enter 发送 · Shift+Enter 换行</span>
            </div>
            <div class="composer-right">
              <span v-if="onlineModel" class="model-badge" title="云端 Agent 模型（只读）">{{ onlineModel }}</span>
              <button class="icon-btn voice-btn" disabled title="语音输入需桌面端（ASR）">
                <el-icon :size="15"><Microphone /></el-icon>
              </button>
              <button v-if="!streaming" class="send-btn" :disabled="(!input.trim() && !pendingAttachments.length) || readonly" title="发送" @click="send">
                <el-icon :size="15"><CaretRight /></el-icon>
              </button>
              <button v-else class="send-btn stop" title="停止" @click="stop"><span class="stop-square" /></button>
            </div>
          </div>
        </div>
        <div class="composer-foot">
          <div class="foot-left">
            <el-dropdown trigger="click" :disabled="streaming" @command="onEntry">
              <button class="foot-chip" :disabled="streaming">在线 · {{ entryLabel }}<span class="foot-caret">▾</span></button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="company">在线 · 零号员工 <el-icon v-if="entryScope === 'company'" class="mode-check"><Check /></el-icon></el-dropdown-item>
                  <el-dropdown-item command="personal">在线 · 员工助手 <el-icon v-if="entryScope === 'personal'" class="mode-check"><Check /></el-icon></el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-dropdown trigger="click" :disabled="streaming" @command="(c: 'default' | 'smart' | 'auto') => (permMode = c)">
              <button class="foot-chip" :disabled="streaming" :title="PERM_MODES[permMode].hint">
                权限 · {{ permLabel }}<span class="foot-caret">▾</span>
              </button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item v-for="(info, key) in PERM_MODES" :key="key" :command="key">
                    {{ info.label }}
                    <el-icon v-if="permMode === key" class="mode-check"><Check /></el-icon>
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
          <span class="foot-hint">由{{ entryLabel }}云端执行</span>
        </div>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.ask-bar {
  border: 1px solid #f0c36d;
  background: #fffbe8;
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 8px;
  font-size: 13px;
}
.readonly-bar {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 8% 0;
  font-size: 12.5px;
  color: var(--warn, #b8822a);
}
.sess-item {
  padding: 6px 8px; border-radius: 8px; cursor: pointer;
  font-size: 12px; color: var(--text-2); margin-bottom: 2px;
  overflow: hidden;
}
.sess-item:hover { background: var(--bg-hover); }
.sess-item.active { background: var(--el-fill-color-darker); color: var(--text); }
.sess-top { display: flex; align-items: center; gap: 6px; min-width: 0; }
.sess-ch { flex: none; }
.sess-id {
  flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-family: Consolas, monospace; font-size: 12px;
}
.sess-meta {
  display: flex; align-items: center; gap: 8px;
  font-size: 11px; color: var(--text-3); margin-top: 3px;
}
.sess-item.active .sess-meta { color: var(--text-3); }
.sess-run { color: var(--warn); }
.ask-q { display: flex; align-items: center; gap: 8px; color: #7a5c00; }
/* 时间线流 */
.flow-block { margin: 2px 0; }
.md-plain { white-space: pre-wrap; word-break: break-word; }
/* 多次输出的正文之间要换行(工具插入被移除后相邻 text 块也保持段落间距) */
.flow-block .md + .flow-block .md { margin-top: 10px; }
.caret-line { color: var(--text-2); margin-top: 2px; }
.caret {
  color: var(--accent, #409eff);
  animation: caret-blink 0.9s step-end infinite;
}
@keyframes caret-blink { 50% { opacity: 0; } }
/* 工具调用卡片(对齐桌面端 .tool-card) */
.tool-chip {
  display: flex; align-items: center; gap: 10px;
  background: var(--el-fill-color-light);
  border: none;
  border-radius: 12px;
  padding: 8px 12px;
  margin: 6px 0;
  font-size: 13px;
  color: var(--text);
}
.tool-chip .tool-name { font-family: Consolas, 'Courier New', monospace; font-weight: 600; font-size: 13px; }
.chip-running { color: var(--el-color-primary); font-size: 11px; animation: blink 1s infinite; }
@keyframes blink { 50% { opacity: 0.2; } }
.tools-sum {
  margin-top: 6px; font-size: 11.5px; color: var(--text-3);
}
/* 思考过程折叠(对齐桌面端 .reasoning) */
.reasoning { margin-bottom: 8px; }
.reasoning-summary {
  font-size: 12.5px; color: var(--text-2); cursor: pointer; list-style: none;
  display: inline-flex; align-items: center; gap: 4px;
}
.reasoning-summary::-webkit-details-marker { display: none; }
.reasoning-summary::before { content: '▸'; transition: transform 0.15s; }
.reasoning[open] .reasoning-summary::before { transform: rotate(90deg); }
.reasoning-text {
  font-size: 12.5px; color: var(--text-2); white-space: pre-wrap;
  margin-top: 4px; padding-left: 4px;
}
.agent-card {
  border: 1px solid var(--border, #e5e7eb);
  border-left: 3px solid #409eff;
  border-radius: 8px;
  padding: 6px 10px;
  background: var(--fill, #f7f8fa);
  margin: 4px 0;
}
.agent-card.running { border-left-color: #f59e0b; }
.agent-head {
  display: flex; align-items: center; gap: 6px;
  cursor: pointer; user-select: none; padding: 2px 0;
}
.chev { transition: transform 0.15s; color: var(--text-3); }
.chev.open { transform: rotate(90deg); }
.agent-name { font-size: 12.5px; font-weight: 600; }
.agent-tools { color: var(--text-3); font-size: 11px; margin-left: auto; }
.agent-body { margin-top: 4px; }
.agent-body .flow-block + .flow-block { margin-top: 4px; }
.agent-stream {
  font-size: 13px;
  color: var(--text-1);
  line-height: 1.65;
  word-break: break-word;
  border-left: 2px solid #d0d7de;
  padding-left: 8px;
  margin-top: 2px;
}
.proc-hint { color: var(--text-2, #555); font-size: 13px; margin-top: 4px; }
.pending-images { display: flex; flex-wrap: wrap; gap: 8px; padding: 4px 8% 8px; align-items: center; }
.pending-chip :deep(.att-img) { max-width: 40px; max-height: 40px; }
.msg-images { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
.pending-chip {
  display: inline-flex; align-items: center; gap: 6px; max-width: 220px;
  padding: 4px 8px; border-radius: 8px; font-size: 12px;
  background: var(--el-fill-color-light, #f2f3f5); color: var(--text-1, #333);
  border: 1px solid var(--border, #e5e7eb);
}
.pending-chip .chip-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pending-chip .thumb-x {
  border: none; background: transparent; cursor: pointer; font-size: 14px; line-height: 1;
  color: var(--text-3, #999); padding: 0;
}
.pending-chip .thumb-x:hover { color: var(--el-color-danger, #e5534b); }
</style>
