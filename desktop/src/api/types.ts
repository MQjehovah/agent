export interface AppConfig {
  agentUrl: string
  ragUrl: string
  marketUrl: string
  routerUrl: string
  routerAdminUrl: string
  /** 企业 SSO(OIDC)Issuer 地址(与主进程 store.ts 同步,留空则读环境变量 OIDC_ISSUER) */
  oidcIssuer: string
  /** 企业 SSO(OIDC)客户端 ID(与主进程 store.ts 同步) */
  oidcClientId: string
  /** 企业 SSO(OIDC)客户端密钥(与主进程 store.ts 同步,留空则读环境变量 OIDC_CLIENT_SECRET) */
  oidcClientSecret: string
  theme: 'dark' | 'light'
  /** 本地 agent 默认模型(与主进程 store.ts 同步) */
  localModel: string
  /** 本地模式默认工作区目录(与主进程 store.ts 同步) */
  defaultWorkspace: string
  /** 关闭主窗口时最小化到托盘(与主进程 store.ts 同步,默认开) */
  closeToTray: boolean
  /** 全局快速提问快捷键(Electron accelerator;空串=不注册) */
  quickHotkey: string
  /** 开机自启(与主进程 store.ts 同步;仅打包版生效) */
  launchAtLogin: boolean
  /** 流式回答结束时发系统通知(与主进程 store.ts 同步,默认开) */
  notifyOnFinish: boolean
  /** 通知时播放系统提示音(与主进程 store.ts 同步,默认关) */
  notifySound: boolean
  /** 启动后自动静默检查更新(与主进程 store.ts 同步,默认开) */
  autoCheckUpdate: boolean
  /** 内网更新源地址(企业 enterprise.json 注入,留空则禁用自动更新) */
  updateFeedUrl: string
  /** 语音转写(ASR)地址(企业 enterprise.json 注入,留空则语音输入禁用;OpenAI 兼容) */
  asrUrl: string
  /** 在线对话权限模式: default 每次询问 / smart 必要时询问 / auto 完全访问 */
  permissionMode?: 'default' | 'smart' | 'auto'
  /** 本地 agent 工具搜索(渐进披露)模式: auto 工具多时(>40)渐进 / always 始终 / off 关闭(全量加载) */
  localAgentToolSearch: 'auto' | 'always' | 'off'
}

/** 自动更新状态快照(主进程 app:update:* 与 desktop:update-status) */
export interface UpdateStatusPayload {
  event: 'unconfigured' | 'idle' | 'checking' | 'available' | 'not-available' | 'downloaded' | 'error'
  message: string
  version?: string
}

export interface AgentUser {
  id: number | string
  name: string
  role: string
  department?: string
}

export interface LoginResponse {
  token: string
  user: AgentUser
}

export interface SessionInfo {
  id: string
  /** 对话根(web:{uid}:{rand} / dingtalk:{uid}:{rand} 等) */
  agent_id?: string
  user_id?: string
  /** 来源渠道:web / dingtalk / other */
  channel?: string
  /** 渠道细类(服务端): web / dingtalk / dingtalk_group / other, 供区分私聊与群 */
  channel_kind?: string
  /** 消息条数(服务端字段名为 messages) */
  messages?: number
  thread_count?: number
  first_accessed?: string | number
  last_accessed?: string | number
  /** 兼容旧字段(若服务端提供) */
  /** 用户自定义标题(重命名)与置顶标记 */
  title?: string
  pinned?: number
  created_at?: string | number
  message_count?: number
  is_streaming?: boolean
}

/** 本地会话元数据(主进程 session store 落盘形状,经 localagent:sessions:* 返回) */
export interface LocalSessionMeta {
  id: string
  mode: 'local'
  title: string
  model: string
  workspace: string
  systemPrompt?: string
  /** 人设名(市场安装的 agent;创建时传入,列表/元信息回读) */
  personaName?: string
  /** 可选人设:来自市场 agent 能力,chat 时 prompt 作为 systemPrompt 前缀 */
  persona?: { name: string; prompt: string }
  createdAt: number
  updatedAt: number
  /** 临时会话(仅本地): 退出后自动删除, 不落历史 */
  ephemeral?: boolean
}

/** 本地模式可安装/浏览的四种市场能力类型(workflow 等由市场侧消费,不进列表) */
export type MarketCapabilityType = 'agent' | 'tool' | 'skill' | 'mcp'

/** 能力运行规格(market §3 契约;全部可选以兼容旧市场,缺失时回退 distribution 推导) */
export interface CapabilityRuntime {
  /** 可云端托管(distribution != local) */
  cloud?: boolean
  /** 可本地安装(distribution != remote) */
  local?: boolean
  /** 推荐安装方式:remote→cloud、local→local、both→cloud */
  recommended?: 'cloud' | 'local'
  transport?: string
  command?: string
  args?: string[]
  url?: string
  env?: Record<string, string>
  tool_count?: number
  risk?: string
  dependencies?: Array<{ name?: string; type?: string }>
}

/** 市场目录中单个能力(浏览用;portal /api/capabilities 条目容错映射,多余字段忽略) */
export interface MarketCapabilityLite {
  id: string
  name: string
  type: MarketCapabilityType
  version: string
  description?: string
  /** 以下为 portal 条目可能附带字段(容错,缺失时 UI 自行降级) */
  author_name?: string
  category?: string
  usage_count?: number
  avg_rating?: number
  rating_count?: number
  install_policy?: string
  tags?: string[]
  /** 分发方式(仅 mcp 有意义):local=本地安装、remote=云端托管、both/缺失=双模式可选 */
  distribution?: string
  /** 运行规格(优先于 distribution;缺失回退) */
  runtime?: CapabilityRuntime
}

/** 本地已安装能力清单条目(localagent:market:listInstalled) */
export interface MarketInstalledItem {
  type: MarketCapabilityType
  name: string
  version?: string
  description?: string
  /** mcp 条目当前模式:platform=平台桥接、local=本地安装 */
  mode?: 'platform' | 'local'
}

/** 本地已装 agent 人设(localagent:personas:list) */
export interface MarketPersona {
  name: string
  description?: string
}

/** 模型发起的工具调用(持久化在 assistant 消息上) */
export interface LocalToolCall {
  id: string
  name: string
  arguments: string
}

/** 本地会话消息(localagent:messages 返回的 jsonl 存储形状) */
export interface LocalStoredMessage {
  role: 'user' | 'assistant' | 'tool'
  content: string
  toolCalls?: LocalToolCall[]
  toolCallId?: string
  name?: string
  ts: number
}

export interface HistoryMessage {
  role: string
  content: string
  /** agent 历史: 工具调用(assistant 消息) */
  tool_calls?: Array<{
    id?: string
    name?: string
    function?: { name?: string; arguments?: string }
  }>
  /** agent 历史: 工具结果消息的归属调用 id */
  tool_call_id?: string
  toolCallId?: string
  /** 工具结果消息里的工具名 */
  name?: string
  /** 深度思考内容 */
  reasoning_content?: string
}

export interface ToolEventData {
  content?: string
  name?: string
  result?: unknown
  agent_name?: string
  agent_type?: string
  [key: string]: unknown
}

/** agent /api/chat/stream 的 SSE 事件(type 即 HookEvent 值或专用事件) */
export interface ChatStreamEvent {
  type:
    | 'token'
    | 'reasoning'
    | 'heartbeat'
    | 'done'
    | 'error'
    | 'tool_start'
    | 'tool_result'
    | 'round_start'
    | 'subagent_start'
    | 'subagent_result'
    | 'subagent_chat_event'
    /** agent 侧子代理流式文本的实际事件名(server.py: SUBAGENT_CHAT_EVENT -> subagent_token) */
    | 'subagent_token'
    | 'subagent_tool_start'
    | 'subagent_tool_result'
    | 'subagent_round_start'
    /** agent 反问(权限确认/ask_user), 需 POST /api/chat/answer 回答 */
    | 'ask'
  content?: string
  data?: ToolEventData
  /** ask 事件字段(扁平形式) */
  ask_id?: string
  question?: string
  options?: string[]
  default?: string
}

/** Wiki 目录中的单页元信息(GET /api/wiki) */
export interface WikiPageMeta {
  id: string
  title: string
  summary?: string
  updated_at: string
}

/** Wiki 目录里的一个分类 */
export interface WikiCategory {
  name: string
  pages: WikiPageMeta[]
}

/** GET /api/wiki 目录响应(category 名可能是「未分类」) */
export interface WikiIndex {
  total: number
  categories: WikiCategory[]
  running: boolean
}

/** Wiki 页面的来源笔记(只读展示) */
export interface WikiSource {
  id: string
  title: string
}

/** GET /api/wiki/{page_id} 单页响应 */
export interface WikiPage {
  id: string
  title: string
  category: string
  content: string
  summary: string
  sources: WikiSource[]
  updated_at: string
}

export interface UsageBucket {
  tokensIn: number
  tokensOut: number
  tokens: number
  cost: number
}

export interface UsageModelRow {
  name: string
  today: UsageBucket
  month: UsageBucket
  dailyQuota: number
  monthlyQuota: number
}

export interface UsageSummary {
  balance: number
  rateLimit: number
  quota: { daily: number; monthly: number }
  today: UsageBucket
  month: UsageBucket
  models: UsageModelRow[]
  /** 模型数超过分解上限时为 true */
  truncated: boolean
  fetchedAt: string
}
