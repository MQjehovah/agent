import { join } from 'node:path'

/**
 * 主进程配置的纯逻辑部分(不依赖 Electron,便于单测):
 * AppConfig 形状、默认值构造与「读取 → 合并 → 写回」的持久化 store。
 * electron/main/store.ts 负责注入 Electron 的路径与文件读写。
 */
export interface AppConfig {
  agentUrl: string
  ragUrl: string
  marketUrl: string
  routerUrl: string
  /** router admin 地址:SSO 登录后同域自取 router 凭据(/api/me/*) */
  routerAdminUrl: string
  /** agent 服务间专用凭证(与 .34 agent 的 AGENT_SERVICE_TOKEN 一致) */
  agentServiceToken: string
  /** 企业 SSO(OIDC)Issuer 地址,留空则读环境变量 OIDC_ISSUER;无内置兜底 */
  oidcIssuer: string
  /** 企业 SSO(OIDC)客户端 ID,留空则读环境变量 OIDC_CLIENT_ID 或默认 dashboard-gateway */
  oidcClientId: string
  /** 企业 SSO(OIDC)客户端密钥(可选,仅机密客户端需要;留空则读环境变量 OIDC_CLIENT_SECRET) */
  oidcClientSecret: string
  theme: 'dark' | 'light'
  /** 本地 agent 默认模型 */
  localModel: string
  /** 本地模式默认工作区目录(新建本地会话免弹窗) */
  defaultWorkspace: string
  /** 关闭主窗口时最小化到托盘(默认开;退出走托盘菜单) */
  closeToTray: boolean
  /** 全局快速提问快捷键(Electron accelerator;空串表示不注册) */
  quickHotkey: string
  /** 开机自启(仅打包版写入系统;开发模式只记录配置) */
  launchAtLogin: boolean
  /** 流式回答结束且主窗口未聚焦时发系统通知 */
  notifyOnFinish: boolean
  /** 发通知时播放系统提示音 */
  notifySound: boolean
  /** 启动后自动静默检查更新(默认开;更新源未配置时不生效) */
  autoCheckUpdate: boolean
  /** 内网更新源地址(generic provider;企业 enterprise.json 注入,留空则禁用自动更新) */
  updateFeedUrl: string
  /** 语音转写(ASR)地址:OpenAI 兼容 base 或完整端点(企业 enterprise.json 注入,留空则语音输入禁用) */
  asrUrl: string
  /** 在线对话权限模式(渲染层消费;主进程只持久化,缺省视为 default) */
  permissionMode?: 'default' | 'smart' | 'auto'
  /**
   * 本地 agent 渐进披露(工具搜索)模式:
   * auto=已注册远程工具数 > 40 时自动渐进, always=总是渐进, off=关闭(每轮全量)。
   * 设置页可改; 渲染层传入的非法值由 filterUserConfigPatch 拒绝, 磁盘脏值按 auto 处理
   */
  localAgentToolSearch: 'auto' | 'always' | 'off'
}

export interface DefaultConfigInput {
  /** home 目录(主进程传 app.getPath('home')) */
  home: string
  env: Record<string, string | undefined>
}

export function buildDefaultConfig(input: DefaultConfigInput): AppConfig {
  const { home, env } = input
  return {
    agentUrl: 'https://ai.xzrobot.com/agent',
    ragUrl: 'https://ai.xzrobot.com/rag',
    marketUrl: 'https://ai.xzrobot.com/market',
    routerUrl: 'https://ai.xzrobot.com/router',
    routerAdminUrl: env.ROUTER_ADMIN_URL ?? 'https://ai.xzrobot.com/router',
    agentServiceToken: env.AGENT_SERVICE_TOKEN ?? '',
    oidcIssuer: env.OIDC_ISSUER ?? 'https://auth.xzrobot.com',
    oidcClientId: env.OIDC_CLIENT_ID ?? 'dashboard-gateway',
    oidcClientSecret: env.OIDC_CLIENT_SECRET ?? '',
    theme: 'dark',
    localModel: 'deepseek-flash',
    defaultWorkspace: join(home, 'local-workspace'),
    closeToTray: true,
    quickHotkey: 'Alt+Space',
    launchAtLogin: false,
    notifyOnFinish: true,
    notifySound: false,
    autoCheckUpdate: true,
    updateFeedUrl: '',
    asrUrl: '',
    localAgentToolSearch: 'auto'
  }
}

/**
 * 渲染层可写配置键(用户偏好):config:set 只接受这些键。
 * 企业/敏感字段(OIDC、服务地址、agentServiceToken、updateFeedUrl 等)不在此列,
 * 只能由 seed 或主进程内部经 updateConfig 直写,渲染层传入一律忽略。
 */
export const USER_CONFIG_KEYS = [
  'theme',
  'localModel',
  'defaultWorkspace',
  'permissionMode',
  'localAgentToolSearch',
  'closeToTray',
  'quickHotkey',
  'launchAtLogin',
  'notifyOnFinish',
  'notifySound',
  'autoCheckUpdate'
] as const

export type UserConfigKey = (typeof USER_CONFIG_KEYS)[number]

/** 三档工具搜索模式的合法取值（与 tool-search.ts ToolSearchMode 对齐） */
const TOOL_SEARCH_MODES: readonly string[] = ['auto', 'always', 'off']

/**
 * 需要类型约束的白名单键的取值校验：返回 false 视为非法（拒绝写入并记录），
 * 未列出的键沿用既有行为（仅白名单过滤，不做值校验）。
 */
const USER_CONFIG_VALIDATORS: Partial<Record<UserConfigKey, (value: unknown) => boolean>> = {
  localAgentToolSearch: (v) => typeof v === 'string' && TOOL_SEARCH_MODES.includes(v)
}

export interface FilteredConfigPatch {
  /** 只含白名单键的补丁 */
  patch: Partial<AppConfig>
  /** 被忽略的键(供调用方告警日志) */
  ignored: string[]
}

/** 过滤渲染层补丁:白名单外的键一律丢弃并记录,不抛错(不影响 UI 正常保存) */
export function filterUserConfigPatch(input: Partial<AppConfig> | null | undefined): FilteredConfigPatch {
  const patch: Record<string, unknown> = {}
  const ignored: string[] = []
  for (const [key, value] of Object.entries(input ?? {})) {
    if (!(USER_CONFIG_KEYS as readonly string[]).includes(key)) {
      if (value !== undefined) ignored.push(key)
      continue
    }
    const validate = USER_CONFIG_VALIDATORS[key as UserConfigKey]
    if (validate && !validate(value)) {
      // 合法键但非法值:拒绝写入(避免脏值落盘),记录供调用方告警;读取侧仍有 auto 兜底
      ignored.push(key)
      continue
    }
    patch[key] = value
  }
  return { patch: patch as Partial<AppConfig>, ignored }
}

export interface ConfigStoreDeps {
  /** 读取 config.json 原始文本;文件不存在返回 null */
  readText(): string | null
  /** 覆盖写入 config.json(目录由调用方保证存在) */
  writeText(text: string): void
}

export interface ConfigStore<T> {
  /** 合并默认值后的当前配置(内存缓存) */
  get(): T
  /** 磁盘原始内容(不合并默认值),损坏/缺失返回 {} */
  stored(): Partial<T>
  /** 合并补丁并落盘,返回新配置 */
  update(patch: Partial<T>): T
}

export function createConfigStore<T extends object>(defaults: T, deps: ConfigStoreDeps): ConfigStore<T> {
  let cached: T | null = null

  function stored(): Partial<T> {
    try {
      const text = deps.readText()
      return text ? (JSON.parse(text) as Partial<T>) : {}
    } catch {
      // 配置文件缺失或损坏时视为空,并在下次保存时覆盖
      return {}
    }
  }

  function get(): T {
    if (!cached) cached = { ...defaults, ...stored() }
    return cached
  }

  function update(patch: Partial<T>): T {
    const next = { ...get(), ...patch }
    cached = next
    deps.writeText(JSON.stringify(next, null, 2))
    return next
  }

  return { get, stored, update }
}
