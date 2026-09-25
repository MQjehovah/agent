/**
 * agent 内核共享类型定义。
 * 工具、会话消息与内核事件的统一契约，供 loop/session/tools 等模块复用。
 */

/** 工具执行上下文：工作区根目录与会话标识 */
export interface ToolContext {
  workspace: string
  sessionId: string
}

/** 工具执行结果 */
export interface ToolResult {
  ok: boolean
  output: string
}

/** OpenAI function calling 形式的工具描述 */
export interface OpenAiTool {
  type: 'function'
  function: { name: string; description: string; parameters: Record<string, unknown> }
}

export interface ToolDefinition {
  name: string
  description: string
  /** 'read' 自动放行;'write' 需权限确认 */
  kind: 'read' | 'write'
  /** JSON Schema 形式的参数定义 */
  parameters: Record<string, unknown>
  execute(args: Record<string, unknown>, ctx: ToolContext): Promise<ToolResult>
}

/** 模型发起的工具调用（arguments 为 JSON 字符串） */
export interface ToolCall {
  id: string
  name: string
  arguments: string
}

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string
  toolCalls?: ToolCall[]
  toolCallId?: string
  name?: string
}

/** 内核向 UI 派发的事件流 */
export type AgentEvent =
  | { type: 'token'; text: string }
  /** 推理模型思维链增量(仅用于界面展示) */
  | { type: 'reasoning'; text: string }
  | { type: 'tool_call'; name: string; args: string }
  | { type: 'tool_result'; name: string; output: string; ok: boolean }
  | { type: 'permission_request'; requestId: string; sessionId: string; tool: string; summary: string; streamId?: string }
  | { type: 'done' }
  | { type: 'error'; message: string }
