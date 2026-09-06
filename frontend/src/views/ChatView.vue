<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref } from 'vue'
import { streamChat } from '../api'
import MarkdownIt from 'markdown-it'

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

interface ToolTrace { name: string; done: boolean }
interface Msg {
  role: 'user' | 'assistant'
  content: string
  reasoning: string
  tools: ToolTrace[]
  error: string
}

const messages = ref<Msg[]>([])
const input = ref('')
const streaming = ref(false)
const sessionId = ref('')
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
  const sid = sessionId.value || undefined
  streamChat(
    { message: text, session_id: sid },
    (ev) => {
      switch (ev.type) {
        case 'token':
          reply.content += ev.content ?? ''
          break
        case 'reasoning':
          reply.reasoning += ev.content ?? ''
          break
        case 'tool_start':
        case 'subagent_tool_start':
          reply.tools.push({ name: String(ev.data?.name || ev.data?.agent_name || 'tool'), done: false })
          break
        case 'tool_result':
        case 'subagent_tool_result': {
          const name = String(ev.data?.name || ev.data?.agent_name || 'tool')
          const hit = [...reply.tools].reverse().find((t) => t.name === name && !t.done)
          if (hit) hit.done = true
          break
        }
        case 'done':
          if (ev.content) reply.content = ev.content
          if (ev.data?.session_id) sessionId.value = String(ev.data.session_id)
          break
        case 'error':
          reply.error = ev.content ?? '未知错误'
          break
      }
      scrollBottom()
    },
    abort.signal
  )
    .catch((err) => {
      if ((err as Error).name !== 'AbortError') reply.error = (err as Error).message
    })
    .finally(() => {
      streaming.value = false
      abort = null
      // done 事件里没带 session_id 时,从会话列表兜底取最新
      if (!sessionId.value) sessionId.value = ''
      scrollBottom()
    })
}

function stop() {
  abort?.abort()
}

onBeforeUnmount(() => abort?.abort())
</script>

<template>
  <div class="chat">
    <div ref="scrollRef" class="chat-scroll">
      <div v-if="messages.length === 0" style="text-align: center; margin-top: 12vh; color: var(--el-text-color-secondary)">
        <h2 style="font-weight: 600">有什么可以帮你?</h2>
        <p>我是公司数字员工:回答问题、查知识、处理工单、定时任务。</p>
      </div>

      <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
        <div class="bubble">
          <div v-if="m.reasoning" style="font-size: 12px; color: var(--el-text-color-secondary); margin-bottom: 4px">
            <el-icon style="vertical-align: middle" class="chip-running"><component :is="''" v-if="false" /></el-icon>
            <details><summary>思考过程</summary><div style="white-space: pre-wrap">{{ m.reasoning }}</div></details>
          </div>
          <div v-if="m.tools.length" class="tools">
            <el-tag v-for="(t, ti) in m.tools" :key="ti" size="small" :type="t.done ? 'success' : 'warning'">
              {{ t.name + (t.done ? ' ✓' : ' …') }}
            </el-tag>
          </div>
          <div v-if="m.role === 'user'" >{{ m.content }}</div>
          <div v-else-if="m.content" class="md" v-html="render(m.content)" />
          <div v-else-if="streaming && i === messages.length - 1" style="color: var(--el-text-color-secondary)">
            <span class="chip-running">●</span> 正在处理…
          </div>
          <el-alert v-if="m.error" :title="m.error" type="error" :closable="false" style="margin-top: 8px" />
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
      <el-button v-if="!streaming" type="primary" :disabled="!input.trim()" @click="send">发送</el-button>
      <el-button v-else type="warning" @click="stop">停止</el-button>
    </div>
  </div>
</template>
