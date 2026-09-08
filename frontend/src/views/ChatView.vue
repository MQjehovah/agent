<script setup lang="ts">
defineOptions({ name: 'ChatView' })
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, post, streamChat } from '../api'
import { channelMeta, dingtalkGroupDisplayName, isDingtalkGroupSession } from '../channel'
import MarkdownIt from 'markdown-it'
import { Promotion, VideoPause, Plus, Delete, Warning, ArrowRight } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

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
      if (m.role === 'user') {
        return { role: 'user' as const, content: m.content, reasoning: '', blocks: [], toolCount: 0, error: '' }
      }
      const a = emptyAssistant()
      if (m.content) a.blocks.push({ kind: 'text', content: m.content, html: '' })
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
  if (!text || streaming.value || readonly.value) return
  input.value = ''
  messages.value.push({ role: 'user', content: text, reasoning: '', blocks: [], toolCount: 0, error: '' })
  const reply: Msg = emptyAssistant()
  messages.value.push(reply)
  streaming.value = true
  procTip.value = '正在处理…'
  scrollBottom()

  abort = new AbortController()
  streamChat(
    { message: text, session_id: sessionId.value || undefined },
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

let pollTimer: number | undefined
onMounted(() => {
  void loadSessions()
  pollTimer = window.setInterval(() => void loadSessions(), 15000)
})
onBeforeUnmount(() => {
  abort?.abort()
  if (pollTimer) window.clearInterval(pollTimer)
})
</script>

<template>
  <div class="chat">
    <aside class="chat-side">
      <el-button class="new-btn" type="primary" plain style="width: 100%" :icon="Plus" @click="newSession">新会话</el-button>
      <div class="sess-list">
        <div v-for="s in sessions" :key="s.id" class="sess-item" :class="{ active: s.id === sessionId }" @click="openSession(s)">
          <div class="sess-top">
            <el-tag size="small" effect="plain" :type="channelMeta(s.channel, s.id).type" class="sess-ch">{{ channelMeta(s.channel, s.id).label }}</el-tag>
            <span class="sess-id" :title="s.id">{{ displayTitle(s) }}</span>
          </div>
          <div class="sess-meta">
            <span>{{ s.message_count ?? 0 }} 条</span>
            <span>{{ shortTime(s.last_accessed || s.created_at) }}</span>
            <span v-if="s.is_streaming" class="sess-run">运行中</span>
          </div>
        </div>
        <div v-if="sessions.length === 0" style="font-size: 12px; color: var(--text-3); padding: 6px">暂无历史会话</div>
      </div>
      <div v-if="sessionId && !readonly" style="display: flex; align-items: center; gap: 6px; padding-top: 8px; border-top: 1px solid var(--border)">
        <span class="mono" style="flex: 1; font-size: 11px; color: var(--text-3); overflow: hidden; text-overflow: ellipsis">{{ sessionId }}</span>
        <el-button size="small" text type="danger" :icon="Delete" @click="newSession" title="结束当前会话" />
      </div>
    </aside>

    <div class="chat-main">
      <div v-if="readonly" class="readonly-bar">
        <el-icon :size="14"><Warning /></el-icon>
        <span>该会话来自「{{ channelMeta(currentSession?.channel || '', currentSession?.id || '').label }}」渠道，仅支持查看历史，请在对应渠道继续对话。</span>
        <el-button size="small" @click="newSession">新建会话</el-button>
      </div>
      <div ref="scrollRef" class="chat-scroll">
        <div v-if="messages.length === 0" style="text-align: center; margin-top: 13vh">
          <h2 style="font-weight: 650; font-size: 22px">有什么可以帮你?</h2>
          <p style="color: var(--text-2); font-size: 13.5px">回答问题 · 查知识 · 处理工单 · 定时任务</p>
        </div>

        <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
          <div class="bubble" style="min-width: 30%">
            <!-- 用户消息：纯文本 -->
            <div v-if="m.role === 'user'">{{ m.content }}</div>

            <!-- 助手消息：按输出顺序的时间线流 -->
            <template v-else>
              <details v-if="m.reasoning" style="margin-bottom: 6px">
                <summary style="font-size: 12px; color: var(--text-2); cursor: pointer">思考过程</summary>
                <div style="font-size: 12px; color: var(--text-2); white-space: pre-wrap; margin-top: 4px">{{ m.reasoning }}</div>
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

      <div class="composer">
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
        <el-input
          v-model="input"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 8 }"
          :disabled="readonly"
          :placeholder="readonly ? '该会话只读，不可发送消息' : '输入任务或问题,Enter 发送,Shift+Enter 换行'"
          resize="none"
          @keydown.enter.exact.prevent="send"
        />
        <el-button v-if="readonly" type="primary" :icon="Promotion" disabled>只读</el-button>
        <el-button v-else-if="!streaming" type="primary" :icon="Promotion" :disabled="!input.trim()" @click="send" />
        <el-button v-else type="warning" :icon="VideoPause" @click="stop" />
      </div>
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
.sess-item.active { background: var(--accent-dim); color: var(--accent); }
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
.tool-chip {
  display: inline-flex; align-items: center; gap: 6px;
  border: 1px solid #e3b0ff; background: #faf3ff; color: #6b3fa0;
  border-radius: 14px; padding: 2px 10px; font-size: 12px; margin: 2px 0;
}
.tool-chip .tool-name { font-weight: 600; font-size: 12px; }
.chip-running { color: #f56c6c; font-size: 10px; animation: blink 1s infinite; }
@keyframes blink { 50% { opacity: 0.2; } }
.tools-sum {
  margin-top: 6px; font-size: 11.5px; color: var(--text-3);
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
</style>
