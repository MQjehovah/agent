<script setup lang="ts">
defineOptions({ name: 'ChatView' })
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, streamChat, getToken } from '../api'
import MarkdownIt from 'markdown-it'
import { Promotion, VideoPause, Plus, Delete } from '@element-plus/icons-vue'

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

interface ToolTrace { name: string; done: boolean; args?: string; result?: string }
interface AgentAct { name: string; done: boolean; content: string; tools: ToolTrace[] }
interface Msg { role: 'user' | 'assistant'; content: string; reasoning: string; tools: ToolTrace[]; agents: AgentAct[]; error: string }
interface SessionRow { id: string; created_at: string; message_count: number; is_streaming: boolean }

const messages = ref<Msg[]>([])
const input = ref('')
const streaming = ref(false)
const procTip = ref('')
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
    procTip.value = ''
    messages.value = (d.messages ?? []).map(m => ({
      role: m.role === 'user' ? 'user' : 'assistant',
      content: m.content, reasoning: '', tools: [], agents: [], error: ''
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
  procTip.value = ''
}

function send() {
  const text = input.value.trim()
  if (!text || streaming.value) return
  input.value = ''
  messages.value.push({ role: 'user', content: text, reasoning: '', tools: [], agents: [], error: '' })
  const reply: Msg = { role: 'assistant', content: '', reasoning: '', tools: [], agents: [], error: '' }
  messages.value.push(reply)
  streaming.value = true
  procTip.value = '正在处理…'
  scrollBottom()

  function markToolDone(list: ToolTrace[], name: string) {
    const hit = [...list].reverse().find(t => t.name === name && !t.done)
    if (hit) hit.done = true
  }

  abort = new AbortController()
  streamChat(
    { message: text, session_id: sessionId.value || undefined },
    (ev) => {
      const data = (ev.data ?? {}) as Record<string, any>
      switch (ev.type) {
        case 'token':
          reply.content += ev.content ?? ''
          procTip.value = ''
          break
        case 'reasoning':
          reply.reasoning += ev.content ?? ''
          if (!reply.content) procTip.value = '思考中…'
          break
        case 'round_start':
          procTip.value = `第 ${data.iteration ?? ''} 轮`
          break
        case 'tool_start':
          reply.tools.push({ name: String(data.name || 'tool'), done: false, args: data.arguments })
          procTip.value = `正在调用工具：${String(data.name || 'tool')}`
          break
        case 'tool_result': {
          const name = String(data.name || 'tool')
          const hit = [...reply.tools].reverse().find(t => t.name === name && !t.done)
          if (hit) { hit.done = true; hit.result = data.result }
          procTip.value = '工具执行完成，继续处理…'
          break
        }
        case 'subagent_start': {
          const name = String(data.agent_name || 'subagent')
          reply.agents.push({ name, done: false, content: '', tools: [] })
          procTip.value = `已派生子代理「${name}」执行`
          break
        }
        case 'subagent_token': {
          const act = reply.agents[reply.agents.length - 1]
          if (act && !act.done) act.content += data.content ?? ev.content ?? ''
          procTip.value = '子代理执行中…'
          break
        }
        case 'subagent_tool_start': {
          const act = reply.agents[reply.agents.length - 1]
          if (act) act.tools.push({ name: String(data.name || 'tool'), done: false, args: data.arguments })
          procTip.value = `子代理正在调用工具：${String(data.name || 'tool')}`
          break
        }
        case 'subagent_tool_result': {
          const act = reply.agents[reply.agents.length - 1]
          if (act) {
            const hit = [...act.tools].reverse().find(t => t.name === String(data.name || 'tool') && !t.done)
            if (hit) { hit.done = true; hit.result = data.result }
          }
          break
        }
        case 'subagent_result': {
          const act = reply.agents[reply.agents.length - 1]
          if (act) act.done = true
          procTip.value = '子代理已结束，汇总结果中…'
          break
        }
        case 'done':
          if (ev.content) reply.content = ev.content
          for (const act of reply.agents) act.done = true
          for (const t of reply.tools) t.done = true
          procTip.value = ''
          break
        case 'error': reply.error = ev.content ?? '未知错误'; procTip.value = ''; break
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

            <div v-if="m.tools.length" class="tool-list">
              <div v-for="(t, ti) in m.tools" :key="ti" class="tool-row" :class="{ active: !t.done }">
                <div class="tool-title">
                  <span class="tool-name mono">{{ t.name }}</span>
                  <el-tag size="small" :type="t.done ? 'success' : 'warning'" class="tool-state">{{ t.done ? '完成' : '运行中…' }}</el-tag>
                  <span v-if="t.done && t.result" class="tool-note">({{ String(t.result).length }} 字符)</span>
                </div>
                <div v-if="t.args" class="tool-meta mono">{{ t.args }}</div>
                <details v-if="t.done && t.result" class="tool-detail">
                  <summary>查看结果</summary>
                  <pre class="tool-result">{{ t.result }}</pre>
                </details>
              </div>
            </div>

            <div v-if="m.agents.length" class="agent-list">
              <div v-for="(a, ai) in m.agents" :key="ai" class="agent-card" :class="{ active: !a.done }">
                <div class="agent-head">
                  <span class="agent-name mono">{{ a.name }}</span>
                  <el-tag size="small" :type="a.done ? 'success' : 'warning'">
                    {{ a.done ? '完成' : '执行中' }}
                  </el-tag>
                </div>
                <div v-if="a.content" class="agent-stream">{{ a.content }}</div>
                <div v-if="a.tools.length" class="tool-list" style="margin-top: 6px">
                  <div v-for="(t, tj) in a.tools" :key="tj" class="tool-row sub" :class="{ active: !t.done }">
                    <div class="tool-title">
                      <span class="tool-name mono">{{ t.name }}</span>
                      <el-tag size="small" :type="t.done ? 'success' : 'info'" class="tool-state">{{ t.done ? '完成' : '运行中…' }}</el-tag>
                    </div>
                    <div v-if="t.args" class="tool-meta mono">{{ t.args }}</div>
                    <details v-if="t.done && t.result" class="tool-detail">
                      <summary>查看结果</summary>
                      <pre class="tool-result">{{ t.result }}</pre>
                    </details>
                  </div>
                </div>
              </div>
            </div>

            <div v-if="m.role === 'user'">{{ m.content }}</div>
            <div v-else-if="m.content" class="md" v-html="render(m.content)" />
            <div v-else-if="streaming && i === messages.length - 1" class="proc-hint">
              <template v-if="procTip"><span class="chip-running">●</span> {{ procTip }}</template>
              <template v-else><span class="chip-running">●</span> 正在处理…</template>
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

<style scoped>
.tool-list { display: flex; flex-direction: column; gap: 6px; margin: 8px 0; }
.tool-row {
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 8px;
  padding: 6px 10px;
  background: var(--fill, #f7f8fa);
  font-size: 12px;
}
.tool-row.active { border-color: #409eff; background: #eef5ff; }
.tool-row.sub { border-left: 3px solid #909399; background: #fafafa; }
.tool-title { display: flex; align-items: center; gap: 8px; }
.tool-name { font-weight: 600; font-size: 12.5px; }
.tool-meta { color: var(--text-2, #555); word-break: break-all; margin-top: 4px; font-size: 11.5px; }
.tool-note { color: var(--text-3, #888); font-size: 11px; }
.tool-state { margin-left: auto; }
.tool-detail { margin-top: 4px; }
.tool-detail summary { cursor: pointer; color: #409eff; font-size: 12px; }
.tool-result {
  margin: 6px 0 0;
  max-height: 180px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 11.5px;
  background: var(--fill, #fff);
  border-radius: 6px;
  padding: 6px 8px;
}
.proc-hint { color: var(--text-2, #555); font-size: 13px; margin-top: 4px; }
.agent-list { display: flex; flex-direction: column; gap: 8px; margin: 8px 0; }
.agent-card {
  border: 1px solid var(--border, #e5e7eb);
  border-left: 3px solid #409eff;
  border-radius: 8px;
  padding: 8px 10px;
  background: var(--fill, #f7f8fa);
}
.agent-card.active { border-left-color: #f59e0b; }
.agent-head { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.agent-name { font-size: 13px; font-weight: 600; }
.agent-stream {
  font-size: 12px;
  color: var(--text-2, #555);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.6;
  max-height: 200px;
  overflow-y: auto;
  border-left: 2px solid #d0d7de;
  padding-left: 8px;
  margin-top: 4px;
}
</style>
