<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, streamChat, getToken } from '../api'
import MarkdownIt from 'markdown-it'
import { Promotion, VideoPause, Plus, Delete } from '@element-plus/icons-vue'

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

interface ToolTrace { name: string; done: boolean }
interface Msg { role: 'user' | 'assistant'; content: string; reasoning: string; tools: ToolTrace[]; error: string }
interface SessionRow { id: string; created_at: string; message_count: number; is_streaming: boolean }

const messages = ref<Msg[]>([])
const input = ref('')
const streaming = ref(false)
const sessionId = ref('')
const sessions = ref<SessionRow[]>([])
const scrollRef = ref<HTMLElement | null>(null)
let abort: AbortController | null = null

function render(text: string) {
  return md.render(text ?? '')
}

function scrollBottom() {
  void nextTick(() => {
    const el = scrollRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

async function loadSessions() {
  try {
    const d = await api<{ sessions: SessionRow[] }>('/api/sessions')
    sessions.value = d.sessions ?? []
  } catch { /* 静默 */ }
}

async function openSession(row: SessionRow) {
  if (streaming.value) return
  try {
    const d = await api<{ messages: Array<{ role: string; content: string }> }>(
      `/api/sessions/${encodeURIComponent(row.id)}/messages`)
    sessionId.value = row.id
    messages.value = (d.messages ?? []).map(m => ({
      role: m.role === 'user' ? 'user' : 'assistant',
      content: m.content, reasoning: '', tools: [], error: ''
    }))
    scrollBottom()
  } catch (e) {
    console.error(e)
  }
}

async function removeSession(row: SessionRow) {
  try { await api(`/api/sessions/${encodeURIComponent(row.id)}`, { method: 'DELETE' }) } catch { return }
  if (sessionId.value === row.id) { sessionId.value = ''; messages.value = [] }
  await loadSessions()
}

function newSession() {
  if (streaming.value) return
  sessionId.value = ''
  messages.value = []
}

function send() {
  const text = input.value.trim()
  if (!text || streaming.value) return
  input.value = ''
  messages.value.push({ role: 'user', content: text, reasoning: '', tools: [], error: '' })
  const reply: Msg = { role: 'assistant', content: '', reasoning: '', tools: [], error: '' }
  messages.value.push(reply)
  streaming.value = true
  scrollBottom()

  abort = new AbortController()
  streamChat(
    { message: text, session_id: sessionId.value || undefined },
    (ev) => {
      switch (ev.type) {
        case 'token': reply.content += ev.content ?? ''; break
        case 'reasoning': reply.reasoning += ev.content ?? ''; break
        case 'tool_start':
        case 'subagent_tool_start':
          reply.tools.push({ name: String(ev.data?.name || ev.data?.agent_name || 'tool'), done: false })
          break
        case 'tool_result':
        case 'subagent_tool_result': {
          const name = String(ev.data?.name || ev.data?.agent_name || 'tool')
          const hit = [...reply.tools].reverse().find(t => t.name === name && !t.done)
          if (hit) hit.done = true
          break
        }
        case 'done':
          if (ev.content) reply.content = ev.content
          break
        case 'error': reply.error = ev.content ?? '未知错误'; break
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
      abort = null
      void loadSessions()
      scrollBottom()
    })
}

function stop() { abort?.abort() }

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
          {{ s.id }}
        </div>
        <div v-if="sessions.length === 0" style="font-size: 12px; color: var(--text-3); padding: 6px">暂无历史会话</div>
      </div>
      <div v-if="sessionId" style="display: flex; align-items: center; gap: 6px; padding-top: 8px; border-top: 1px solid var(--border)">
        <span class="mono" style="flex: 1; font-size: 11px; color: var(--text-3); overflow: hidden; text-overflow: ellipsis">{{ sessionId }}</span>
        <el-button size="small" text type="danger" :icon="Delete" @click="newSession" title="结束当前会话" />
      </div>
    </aside>

    <div class="chat-main">
      <div ref="scrollRef" class="chat-scroll">
        <div v-if="messages.length === 0" style="text-align: center; margin-top: 13vh">
          <h2 style="font-weight: 650; font-size: 22px">有什么可以帮你?</h2>
          <p style="color: var(--text-2); font-size: 13.5px">回答问题 · 查知识 · 处理工单 · 定时任务</p>
        </div>

        <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
          <div class="bubble" style="min-width: 30%">
            <details v-if="m.reasoning" style="margin-bottom: 6px">
              <summary style="font-size: 12px; color: var(--text-2); cursor: pointer">思考过程</summary>
              <div style="font-size: 12px; color: var(--text-2); white-space: pre-wrap; margin-top: 4px">{{ m.reasoning }}</div>
            </details>
            <div v-if="m.tools.length" class="tools">
              <el-tag v-for="(t, ti) in m.tools" :key="ti" size="small" :type="t.done ? 'success' : 'warning'">
                {{ t.name + (t.done ? ' ✓' : ' …') }}
              </el-tag>
            </div>
            <div v-if="m.role === 'user'">{{ m.content }}</div>
            <div v-else-if="m.content" class="md" v-html="render(m.content)" />
            <div v-else-if="streaming && i === messages.length - 1" style="color: var(--text-2); font-size: 13px">
              <span class="chip-running">●</span> 正在处理…
            </div>
            <el-alert v-if="m.error" :title="m.error" type="error" :closable="false" class="err" />
          </div>
        </div>
      </div>

      <div class="composer">
        <el-input
          v-model="input"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 8 }"
          placeholder="输入任务或问题,Enter 发送,Shift+Enter 换行"
          resize="none"
          @keydown.enter.exact.prevent="send"
        />
        <el-button v-if="!streaming" type="primary" :icon="Promotion" :disabled="!input.trim()" @click="send" />
        <el-button v-else type="warning" :icon="VideoPause" @click="stop" />
      </div>
    </div>
  </div>
</template>
