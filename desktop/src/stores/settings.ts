import { defineStore } from 'pinia'
import type { AgentUser, AppConfig, UpdateStatusPayload } from '../api/types'

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    config: {
      agentUrl: 'https://ai.xzrobot.com/agent',
      ragUrl: 'https://ai.xzrobot.com/rag',
      marketUrl: 'https://ai.xzrobot.com/market',
      routerUrl: 'https://ai.xzrobot.com/router',
      routerAdminUrl: 'https://ai.xzrobot.com/router',
      oidcIssuer: '',
      oidcClientId: 'dashboard-gateway',
      oidcClientSecret: '',
      theme: 'dark',
      localModel: 'deepseek-flash',
      defaultWorkspace: '',
      closeToTray: true,
      quickHotkey: 'Alt+Space',
      launchAtLogin: false,
      notifyOnFinish: true,
      notifySound: false,
      autoCheckUpdate: true,
      updateFeedUrl: '',
      asrUrl: '',
      permissionMode: 'default' as 'default' | 'smart' | 'auto',
      localAgentToolSearch: 'auto' as 'auto' | 'always' | 'off'
    } as AppConfig,
    user: null as AgentUser | null,
    loaded: false
  }),

  getters: {
    theme: (s) => s.config.theme,
    /** 在线对话权限模式(缺省「每次询问」) */
    permissionMode: (s): 'default' | 'smart' | 'auto' => s.config.permissionMode ?? 'default',
    /** 本地 agent 工具搜索模式(缺省「自动」) */
    toolSearchMode: (s): 'auto' | 'always' | 'off' => s.config.localAgentToolSearch ?? 'auto',
    /** 登录态由主进程身份决定,渲染层不持有任何令牌 */
    hasUser: (s) => s.user !== null
  },

  actions: {
    async setPermissionMode(mode: 'default' | 'smart' | 'auto') {
      this.config.permissionMode = mode
      await this.save({ permissionMode: mode })
    },
    async setToolSearchMode(mode: 'auto' | 'always' | 'off') {
      this.config.localAgentToolSearch = mode
      await this.save({ localAgentToolSearch: mode })
    },
    async load() {
      this.config = await window.desktop.invoke<AppConfig>('config:get')
      this.user = (await window.desktop.invoke<AgentUser | null>('auth:me')) ?? null
      this.loaded = true
    },

    async save(patch: Partial<AppConfig>) {
      this.config = await window.desktop.invoke<AppConfig>('config:set', patch)
    },

    /** 开机自启开关:主进程写入系统并持久化配置(开发模式只记录配置) */
    async setLoginItem(openAtLogin: boolean) {
      const res = await window.desktop.invoke<{ openAtLogin: boolean }>('app:loginitem:set', { openAtLogin })
      this.config.launchAtLogin = res.openAtLogin
    },

    /** 开机自启真实系统状态(打包版取系统设置,开发模式回退配置期望值) */
    async loginItemState() {
      return window.desktop.invoke<{ openAtLogin: boolean; packaged?: boolean }>('app:loginitem:get')
    },

    /** 全局快捷键注册状态(保存后查询:已生效 / 已关闭 / 被占用等) */
    async hotkeyStatus() {
      return window.desktop.invoke<{ ok: boolean; error?: string; disabled?: boolean }>('app:hotkey:status')
    },

    /** 应用运行信息(关于卡片展示版本) */
    async appInfo() {
      return window.desktop.invoke<{ version: string; electron: string; node: string; packaged: boolean }>('app:info')
    },

    /** 手动检查更新,返回可读状态快照 */
    async checkUpdate() {
      return window.desktop.invoke<UpdateStatusPayload>('app:update:check')
    },

    /** 退出并安装已下载的更新 */
    async installUpdate() {
      return window.desktop.invoke<{ ok: boolean; error?: string }>('app:update:install')
    },

    /** 当前更新状态快照(进入设置页时拉取) */
    async updateStatus() {
      return window.desktop.invoke<UpdateStatusPayload>('app:update:status')
    },

    /** 企业账号 SSO:主进程开系统浏览器完成认证,凭证留在主进程 */
    async loginSso() {
      const res = await window.desktop.invoke<{ user: AgentUser }>('sso:start')
      this.user = res.user
    },

    async logout() {
      await window.desktop.invoke('auth:logout')
      this.user = null
    }
  }
})
