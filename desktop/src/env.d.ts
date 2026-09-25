/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, never>, Record<string, never>, unknown>
  export default component
}

type UpstreamEventType = 'status' | 'chunk' | 'end' | 'error'

declare global {
  interface UpstreamEventPayload {
    streamId: string
    type: UpstreamEventType
    status?: number
    text?: string
    message?: string
    aborted?: boolean
  }

  interface LocalAgentEventPayload {
    /** permission_request 事件可能条件性省略，其余变体必有 */
    streamId?: string
    type: 'token' | 'reasoning' | 'tool_call' | 'tool_result' | 'permission_request' | 'done' | 'error'
    text?: string
    name?: string
    args?: string
    output?: string
    ok?: boolean
    requestId?: string
    sessionId?: string
    tool?: string
    summary?: string
    message?: string
  }

  interface UpdateStatusPayload {
    event: 'unconfigured' | 'idle' | 'checking' | 'available' | 'not-available' | 'downloaded' | 'error'
    message: string
    version?: string
  }

  interface DesktopBridge {
    invoke<T = unknown>(channel: string, payload?: unknown): Promise<T>
    onUpstreamEvent(callback: (event: UpstreamEventPayload) => void): () => void
    onLocalAgentEvent(callback: (event: LocalAgentEventPayload) => void): () => void
    onQuickPrompt(callback: (payload: { text: string }) => void): () => void
    onQuickError(callback: (payload: { message: string }) => void): () => void
    onOpenSession(callback: (payload: { sessionId: string }) => void): () => void
    onNewSession(callback: () => void): () => void
    onUpdateStatus(callback: (payload: UpdateStatusPayload) => void): () => void
  }

  interface Window {
    desktop: DesktopBridge
  }
}

export {}
