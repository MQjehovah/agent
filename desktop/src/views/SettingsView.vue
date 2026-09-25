<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  CircleCheck,
  Download,
  FolderOpened,
  InfoFilled,
  Monitor,
  Platform,
  Refresh,
  Setting,
  SwitchButton,
  UserFilled
} from '@element-plus/icons-vue'
import { useSettingsStore } from '../stores/settings'
import { useChatStore } from '../stores/chat'
import { isValidAccelerator } from '../utils/hotkey'
import type { AppConfig, UpdateStatusPayload } from '../api/types'

const settings = useSettingsStore()
const chat = useChatStore()

const form = reactive({
  theme: 'dark' as 'dark' | 'light',
  localModel: '',
  defaultWorkspace: '',
  closeToTray: true,
  quickHotkey: '',
  launchAtLogin: false,
  notifyOnFinish: true,
  notifySound: false,
  autoCheckUpdate: true
})

/** 持久化字段快照(用于「未保存」判定;主题即刻生效不计入) */
const snapshot = ref('')
const saving = ref(false)
const ssoLoading = ref(false)
const loginItemLoading = ref(false)
/** 全局快捷键注册状态(保存后由主进程回传:已生效 / 已关闭 / 被占用等) */
const hotkeyStatus = ref<{ ok: boolean; error?: string; disabled?: boolean } | null>(null)
/** 是否打包版:开发模式下开机自启等只记录配置不写系统 */
const packaged = ref(true)
/** 已记住的工具（跨会话/重启永久免问，主进程 permission-memory.json） */
const rememberedTools = ref<string[]>([])
const rememberingLoading = ref(false)
const forgettingTool = ref('')
const clearingRemembered = ref(false)
/** 关于卡片:版本与更新状态 */
const appVersion = ref('—')
const electronVersion = ref('')
const updateStatus = ref<UpdateStatusPayload | null>(null)
const updateChecking = ref(false)
const updateInstalling = ref(false)
let offUpdateStatus: (() => void) | null = null

/** 更新源由企业安装包注入,未配置时只降级提示 */
const updateFeedConfigured = computed(() => settings.config.updateFeedUrl.trim().length > 0)

/** 语音转写服务由企业安装包注入,未配置时输入区语音按钮禁用(只读展示,不提供编辑) */
const asrConfigured = computed(() => (settings.config.asrUrl ?? '').trim().length > 0)

/** 工具搜索(渐进披露)模式:改动即时生效并持久化,影响下一次本地对话 */
const toolSearchMode = computed({
  get: () => settings.toolSearchMode,
  set: (mode: 'auto' | 'always' | 'off') => {
    settings.setToolSearchMode(mode).catch(async (err: Error) => {
      ElMessage.error(`保存工具搜索设置失败:${err.message}`)
      await settings.load().catch(() => {})
    })
  }
})

/** 状态文案配色:失败红 / 有新版黄 / 最新或已下载绿 */
const updateStatusClass = computed(() => {
  switch (updateStatus.value?.event) {
    case 'error':
      return 'status-bad'
    case 'available':
      return 'status-warn'
    case 'not-available':
    case 'downloaded':
      return 'status-good'
    default:
      return ''
  }
})

/** 快捷键输入的即时校验(空串=关闭快捷键,合法) */
const hotkeyInputError = computed(() => {
  const value = form.quickHotkey.trim()
  if (!value) return ''
  return isValidAccelerator(value) ? '' : '格式非法,示例:Alt+Space / Ctrl+Shift+A'
})

/** 当前输入与已保存值不一致 → 未保存;否则看主进程保存后的注册状态 */
const hotkeyDirty = computed(() => form.quickHotkey.trim() !== (settings.config.quickHotkey ?? '').trim())

/** 可保存的字段: 企业配置(地址/OIDC/密钥)由安装包注入, 不在设置页展示 */
function payload(): Partial<AppConfig> {
  return {
    localModel: form.localModel.trim(),
    defaultWorkspace: form.defaultWorkspace.trim(),
    closeToTray: form.closeToTray,
    quickHotkey: form.quickHotkey.trim(),
    launchAtLogin: form.launchAtLogin,
    notifyOnFinish: form.notifyOnFinish,
    notifySound: form.notifySound,
    autoCheckUpdate: form.autoCheckUpdate
  }
}

/** 未保存判定:排除即时生效项(launchAtLogin 切换即写入系统并持久化,不应计入 dirty) */
const dirty = computed(() => {
  if (snapshot.value === '') return false
  const current = { ...payload() }
  const saved = JSON.parse(snapshot.value) as Partial<AppConfig>
  delete current.launchAtLogin
  delete saved.launchAtLogin
  return JSON.stringify(current) !== JSON.stringify(saved)
})

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  Object.assign(form, {
    theme: settings.config.theme,
    localModel: settings.config.localModel,
    defaultWorkspace: settings.config.defaultWorkspace,
    closeToTray: settings.config.closeToTray,
    quickHotkey: settings.config.quickHotkey,
    launchAtLogin: settings.config.launchAtLogin,
    notifyOnFinish: settings.config.notifyOnFinish,
    notifySound: settings.config.notifySound,
    autoCheckUpdate: settings.config.autoCheckUpdate
  })
  // 主进程推送的更新状态变化(下载完成等),设置页打开期间实时刷新
  offUpdateStatus = window.desktop.onUpdateStatus((status) => {
    updateStatus.value = status
  })
  try {
    const info = await settings.appInfo()
    appVersion.value = info.version
    electronVersion.value = info.electron
  } catch {
    appVersion.value = '—'
  }
  updateStatus.value = await settings.updateStatus().catch(() => null)
  hotkeyStatus.value = await settings.hotkeyStatus().catch(() => null)
  try {
    // 开机自启显示以系统真实状态为准(开发模式主进程回退配置期望值)
    const loginItem = await settings.loginItemState()
    form.launchAtLogin = loginItem.openAtLogin
    if (loginItem.packaged !== undefined) packaged.value = loginItem.packaged
  } catch {
    packaged.value = true
  }
  // 已记住工具的权限记忆（纯本地状态，随设置页打开刷新）
  await refreshRememberedTools()
  // 系统状态可能覆盖配置期望值,快照放在最后避免误报「未保存」
  snapshot.value = JSON.stringify(payload())
})

/** 保存 */
async function saveAll(quiet = false): Promise<boolean> {
  if (hotkeyInputError.value) {
    ElMessage.error(`快捷键${hotkeyInputError.value}`)
    return false
  }
  saving.value = true
  try {
    await settings.save(payload())
    snapshot.value = JSON.stringify(payload())
    hotkeyStatus.value = await settings.hotkeyStatus().catch(() => null)
    if (!quiet) ElMessage.success('设置已保存')
    return true
  } catch (err) {
    ElMessage.error(`保存失败:${(err as Error).message}`)
    return false
  } finally {
    saving.value = false
  }
}

/** 开机自启:立即写入系统并持久化(开发模式只记录配置,UI 有提示) */
async function onLaunchAtLoginChange(value: boolean | string | number) {
  const next = Boolean(value)
  loginItemLoading.value = true
  try {
    await settings.setLoginItem(next)
    form.launchAtLogin = settings.config.launchAtLogin
  } catch (err) {
    form.launchAtLogin = !next
    ElMessage.error(`设置开机自启失败:${(err as Error).message}`)
  } finally {
    loginItemLoading.value = false
  }
}

/** 主题即时生效并持久化,不等「保存」 */
function onThemeChange(theme: 'dark' | 'light') {
  if (form.theme === theme) return
  form.theme = theme
  void settings.save({ theme })
}

/** 选择本地模式默认工作区 */
async function pickWorkspace() {
  try {
    const pick = await window.desktop.invoke<{ canceled: boolean; path?: string }>('localagent:workspace:pick')
    if (!pick.canceled && pick.path) form.defaultWorkspace = pick.path
  } catch (err) {
    ElMessage.error(`选择目录失败:${(err as Error).message}`)
  }
}

/** 拉取已记住的工具清单（主进程读 permission-memory.json；失败降级为空） */
async function refreshRememberedTools() {
  rememberingLoading.value = true
  try {
    const res = await window.desktop.invoke<{ tools?: string[] }>('localagent:permissions:list-remembered')
    rememberedTools.value = Array.isArray(res?.tools) ? res.tools : []
  } catch {
    rememberedTools.value = []
  } finally {
    rememberingLoading.value = false
  }
}

/** 忘记单个工具：下次执行该工具时重新询问 */
async function forgetRememberedTool(tool: string) {
  if (forgettingTool.value) return
  forgettingTool.value = tool
  try {
    await window.desktop.invoke('localagent:permissions:forget', { tool })
    rememberedTools.value = rememberedTools.value.filter((t) => t !== tool)
    ElMessage.success(`已忘记「${tool}」，下次执行将重新询问`)
  } catch (err) {
    ElMessage.error(`忘记失败:${(err as Error).message}`)
  } finally {
    forgettingTool.value = ''
  }
}

/** 全部清除：二次确认后清空权限记忆 */
async function clearRememberedTools() {
  if (!rememberedTools.value.length || clearingRemembered.value) return
  try {
    await ElMessageBox.confirm(
      `清空后这 ${rememberedTools.value.length} 个工具将重新询问，确定清空？`,
      '全部清除已记住的工具',
      { type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  clearingRemembered.value = true
  try {
    const res = await window.desktop.invoke<{ ok: true; count: number }>('localagent:permissions:clear-remembered')
    rememberedTools.value = []
    ElMessage.success(`已清除 ${res?.count ?? 0} 个已记住的工具`)
  } catch (err) {
    ElMessage.error(`清除失败:${(err as Error).message}`)
  } finally {
    clearingRemembered.value = false
  }
}

/** 手动检查更新:主进程返回可读状态(未配置源/检查中/最新/发现新版本/失败) */
async function checkUpdate() {
  if (updateChecking.value) return
  updateChecking.value = true
  try {
    const status = await settings.checkUpdate()
    updateStatus.value = status
    if (status.event === 'error' || status.event === 'unconfigured') {
      ElMessage.warning(status.message)
    } else if (status.event === 'available') {
      ElMessage.info(`${status.message}，正在后台下载`)
    } else {
      ElMessage.success(status.message)
    }
  } catch (err) {
    ElMessage.error(`检查更新失败:${(err as Error).message}`)
  } finally {
    updateChecking.value = false
  }
}

/** 重启并安装已下载的更新(主进程退出后由安装器接管) */
async function installUpdate() {
  if (updateInstalling.value) return
  updateInstalling.value = true
  try {
    const result = await settings.installUpdate()
    if (!result.ok) ElMessage.error(result.error ?? '安装更新失败')
  } catch (err) {
    ElMessage.error(`安装更新失败:${(err as Error).message}`)
  } finally {
    updateInstalling.value = false
  }
}

onUnmounted(() => {
  offUpdateStatus?.()
  offUpdateStatus = null
})

/** 企业账号 SSO:主进程开系统浏览器,认证后凭证留在主进程 */
async function doSsoLogin() {
  if (ssoLoading.value) return
  if (!(await saveAll(true))) return
  ssoLoading.value = true
  try {
    await settings.loginSso()
    ElMessage.success(`欢迎,${settings.user?.name ?? ''}`)
  } catch (err) {
    ElMessage.error(`企业账号登录失败:${(err as Error).message}`)
  } finally {
    ssoLoading.value = false
  }
}

async function doLogout() {
  try {
    await ElMessageBox.confirm('退出后需重新通过企业账号登录,确定退出?', '退出登录', {
      type: 'warning',
      confirmButtonText: '退出',
      cancelButtonText: '取消'
    })
  } catch {
    return
  }
  await settings.logout()
  chat.newSession()
  ElMessage.success('已退出登录')
}

</script>

<template>
  <div class="settings-view">
    <header class="view-header">
      <div class="view-header-left">
        <span class="view-title">设置</span>
        <span class="view-sub">账号、服务地址与本地模式</span>
      </div>
    </header>

    <div class="settings-body">
      <!-- 账号与企业认证 -->
      <section class="section-card">
        <div class="card-head">
          <el-icon class="card-icon" :size="16"><UserFilled /></el-icon>
          <div>
            <h3 class="card-title">账号与企业认证</h3>
            <p class="card-sub">对话与各系统的身份来自企业统一认证（SSO），凭证只保存在本机主进程</p>
          </div>
        </div>

        <div class="account-row">
          <div class="avatar" :class="{ guest: !settings.hasUser }">
            {{ (settings.user?.name || '?').slice(0, 1).toUpperCase() }}
          </div>
          <div class="account-info">
            <template v-if="settings.hasUser">
              <div class="account-name">
                {{ settings.user?.name }}
                <span class="status-dot" /> 已登录
              </div>
              <div class="account-meta">
                工号 {{ settings.user?.id }}<template v-if="settings.user?.department"> · {{ settings.user?.department }}</template>
                <template v-if="settings.user?.role"> · {{ settings.user?.role }}</template>
              </div>
            </template>
            <template v-else>
              <div class="account-name">未登录</div>
              <div class="account-meta">登录后才能使用对话、能力市场与用量查询</div>
            </template>
          </div>
          <div class="account-actions">
            <el-button type="primary" :loading="ssoLoading" @click="doSsoLogin">
              {{ settings.hasUser ? '重新登录' : '登录企业账号' }}
            </el-button>
            <el-button v-if="settings.hasUser" plain :icon="SwitchButton" @click="doLogout">退出登录</el-button>
          </div>
        </div>
      </section>

      <!-- 本地模式 -->
      <section class="section-card">
        <div class="card-head">
          <el-icon class="card-icon" :size="16"><Monitor /></el-icon>
          <div>
            <h3 class="card-title">本地模式</h3>
            <p class="card-sub">在本机执行任务时使用的模型、工作区与工具搜索（与云端 Agent 相互独立）</p>
          </div>
        </div>

        <el-form label-position="top" class="form">
          <el-form-item label="默认模型">
            <el-input v-model="form.localModel" placeholder="deepseek-flash" />
          </el-form-item>
          <el-form-item label="默认工作区">
            <div class="workspace-row">
              <el-input v-model="form.defaultWorkspace" placeholder="本地模式的默认工作区目录（绝对路径）" class="workspace-input" />
              <el-button :icon="FolderOpened" @click="pickWorkspace">选择目录</el-button>
            </div>
            <div class="field-hint">新建本地会话时默认使用该目录，未设置则每次询问</div>
          </el-form-item>
          <el-form-item label="工具搜索">
            <el-radio-group v-model="toolSearchMode">
              <el-radio-button value="auto">自动（工具多时，&gt;40 个）</el-radio-button>
              <el-radio-button value="always">始终启用</el-radio-button>
              <el-radio-button value="off">关闭</el-radio-button>
            </el-radio-group>
            <div class="field-hint">渐进披露：需要远程能力时模型先搜索再调用；关闭=全量加载</div>
          </el-form-item>
        </el-form>

        <div class="remembered-block" v-loading="rememberingLoading">
          <div class="remembered-head">
            <span class="remembered-title">已记住的工具（{{ rememberedTools.length }}）</span>
            <el-button
              v-if="rememberedTools.length"
              size="small"
              plain
              :loading="clearingRemembered"
              @click="clearRememberedTools"
            >
              全部清除
            </el-button>
          </div>
          <p v-if="!rememberedTools.length" class="field-hint remembered-empty">
            暂无（在权限确认框选择“允许并记住”后会出现在这里）
          </p>
          <div v-else class="remembered-list">
            <div v-for="tool in rememberedTools" :key="tool" class="remembered-row">
              <span class="remembered-name" :title="tool">{{ tool }}</span>
              <el-button
                size="small"
                plain
                :loading="forgettingTool === tool"
                @click="forgetRememberedTool(tool)"
              >
                忘记
              </el-button>
            </div>
          </div>
        </div>
      </section>

      <!-- 外观 -->
      <section class="section-card">
        <div class="card-head">
          <el-icon class="card-icon" :size="16"><Setting /></el-icon>
          <div>
            <h3 class="card-title">外观</h3>
            <p class="card-sub">主题切换即时生效，无需保存</p>
          </div>
        </div>

        <div class="theme-row">
          <button type="button" class="theme-tile" :class="{ on: form.theme === 'dark' }" @click="onThemeChange('dark')">
            <span class="tile-preview dark">
              <span class="tile-side" />
              <span class="tile-main">
                <i /><i /><i />
              </span>
            </span>
            <span class="tile-label">
              深色
              <el-icon v-if="form.theme === 'dark'" :size="14"><CircleCheck /></el-icon>
            </span>
          </button>
          <button type="button" class="theme-tile" :class="{ on: form.theme === 'light' }" @click="onThemeChange('light')">
            <span class="tile-preview light">
              <span class="tile-side" />
              <span class="tile-main">
                <i /><i /><i />
              </span>
            </span>
            <span class="tile-label">
              浅色
              <el-icon v-if="form.theme === 'light'" :size="14"><CircleCheck /></el-icon>
            </span>
          </button>
        </div>
      </section>

      <!-- 桌面 -->
      <section class="section-card">
        <div class="card-head">
          <el-icon class="card-icon" :size="16"><Platform /></el-icon>
          <div>
            <h3 class="card-title">桌面</h3>
            <p class="card-sub">托盘、快速提问、开机自启、系统通知与语音输入</p>
          </div>
        </div>

        <div class="desk-list">
          <div class="desk-row">
            <div class="desk-info">
              <span class="desk-name">关闭窗口时最小化到托盘</span>
              <span class="desk-hint">关闭按钮不退出应用，可从托盘菜单「退出」</span>
            </div>
            <el-switch v-model="form.closeToTray" />
          </div>

          <div class="desk-row">
            <div class="desk-info">
              <span class="desk-name">快速提问快捷键</span>
              <span class="desk-hint">全局生效，唤起置顶小窗；留空则关闭</span>
            </div>
            <div class="desk-control hotkey-control">
              <el-input
                v-model="form.quickHotkey"
                placeholder="Alt+Space"
                :class="{ 'is-invalid': !!hotkeyInputError }"
              />
              <div class="hotkey-status">
                <template v-if="hotkeyInputError">
                  <span class="hotkey-bad">{{ hotkeyInputError }}</span>
                </template>
                <template v-else-if="hotkeyDirty">
                  <el-tag size="small" type="info">未保存</el-tag>
                  <span class="hotkey-note">保存后按新快捷键注册</span>
                </template>
                <template v-else-if="!form.quickHotkey.trim()">
                  <el-tag size="small" type="info">已关闭</el-tag>
                  <span class="hotkey-note">未启用全局快捷键</span>
                </template>
                <template v-else-if="hotkeyStatus">
                  <el-tag v-if="hotkeyStatus.ok" size="small" type="success">已生效</el-tag>
                  <el-tag v-else size="small" type="warning">未生效</el-tag>
                  <span class="hotkey-note">{{ hotkeyStatus.error ?? '' }}</span>
                </template>
              </div>
            </div>
          </div>

          <div class="desk-row">
            <div class="desk-info">
              <span class="desk-name">开机自启</span>
              <span class="desk-hint">
                随系统启动自动打开
                <template v-if="!packaged">（当前为开发模式，仅打包版会真正写入系统）</template>
              </span>
            </div>
            <el-switch v-model="form.launchAtLogin" :loading="loginItemLoading" @change="onLaunchAtLoginChange" />
          </div>

          <div class="desk-row">
            <div class="desk-info">
              <span class="desk-name">回答完成通知</span>
              <span class="desk-hint">窗口未聚焦时，回答结束后发系统通知（点击跳回会话）</span>
            </div>
            <el-switch v-model="form.notifyOnFinish" />
          </div>

          <div class="desk-row">
            <div class="desk-info">
              <span class="desk-name">通知提示音</span>
              <span class="desk-hint">发送通知时播放系统提示音</span>
            </div>
            <el-switch v-model="form.notifySound" :disabled="!form.notifyOnFinish" />
          </div>

          <div class="desk-row">
            <div class="desk-info">
              <span class="desk-name">语音输入服务</span>
              <span class="desk-hint">
                {{ asrConfigured ? settings.config.asrUrl : '未配置，由企业安装包注入 OpenAI 兼容的语音转写服务' }}
              </span>
            </div>
            <el-tag size="small" :type="asrConfigured ? 'success' : 'info'">
              {{ asrConfigured ? '已配置' : '未配置' }}
            </el-tag>
          </div>
        </div>
      </section>

      <!-- 关于 -->
      <section class="section-card">
        <div class="card-head">
          <el-icon class="card-icon" :size="16"><InfoFilled /></el-icon>
          <div>
            <h3 class="card-title">关于</h3>
            <p class="card-sub">Dashboard · 零号员工平台员工端 v{{ appVersion }}</p>
          </div>
        </div>

        <div class="desk-list">
          <div class="desk-row">
            <div class="desk-info">
              <span class="desk-name">当前版本</span>
              <span class="desk-hint">
                v{{ appVersion }}<template v-if="electronVersion"> · Electron {{ electronVersion }}</template>
              </span>
            </div>
            <el-tag size="small" type="info">{{ packaged ? '安装版' : '开发模式' }}</el-tag>
          </div>

          <div class="desk-row">
            <div class="desk-info">
              <span class="desk-name">更新源</span>
              <span class="desk-hint">
                {{ updateFeedConfigured ? settings.config.updateFeedUrl : '未配置，由企业安装包注入内网更新源' }}
              </span>
            </div>
            <el-tag size="small" :type="updateFeedConfigured ? 'success' : 'info'">
              {{ updateFeedConfigured ? '已配置' : '未配置' }}
            </el-tag>
          </div>

          <div class="desk-row">
            <div class="desk-info">
              <span class="desk-name">更新状态</span>
              <span class="desk-hint" :class="updateStatusClass">{{ updateStatus?.message ?? '尚未检查更新' }}</span>
            </div>
            <div class="desk-control update-actions">
              <el-button size="small" :icon="Refresh" :loading="updateChecking" @click="checkUpdate">检查更新</el-button>
              <el-button
                v-if="updateStatus?.event === 'downloaded'"
                size="small"
                type="primary"
                :icon="Download"
                :loading="updateInstalling"
                @click="installUpdate"
              >
                重启安装
              </el-button>
            </div>
          </div>

          <div class="desk-row">
            <div class="desk-info">
              <span class="desk-name">自动检查更新</span>
              <span class="desk-hint">启动后延迟静默检查，下载完成后提示重启安装（保存后下次启动生效）</span>
            </div>
            <el-switch v-model="form.autoCheckUpdate" :disabled="!updateFeedConfigured" />
          </div>
        </div>

        <ul class="about-list">
          <li>对话能力由 Agent 执行引擎提供，登录后即可使用</li>
          <li>知识库、能力市场在左侧导航；用量与账号信息见<strong>个人中心</strong></li>
          <li>企业账号、网关密钥与用量查询均依赖上方地址与密钥配置</li>
        </ul>
      </section>
    </div>

    <!-- 吸底操作条 -->
    <footer class="action-bar">
      <div class="bar-left">
        <template v-if="dirty">
          <span class="dirty-dot" />
          <span class="bar-text">有未保存的修改</span>
        </template>
        <template v-else>
          <el-icon class="bar-icon" :size="14"><CircleCheck /></el-icon>
          <span class="bar-text">已是最新</span>
        </template>
      </div>
      <div class="bar-right">
        <el-button type="primary" :loading="saving" :disabled="!dirty" @click="saveAll()">保存</el-button>
      </div>
    </footer>
  </div>
</template>

<style scoped>
.settings-view {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.view-sub {
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}

.settings-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 18px 8px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.section-card {
  border: 1px solid var(--el-border-color-lighter);
  background: var(--el-bg-color);
  border-radius: 14px;
  padding: 18px 20px;
}

.card-head {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin-bottom: 14px;
}

.card-icon {
  margin-top: 2px;
  color: var(--el-color-primary);
}

.card-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.card-sub {
  margin: 3px 0 0;
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}

/* ---- 账号卡 ---- */
.account-row {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 12px 14px;
  border-radius: 12px;
  background: var(--el-fill-color-light);
}

.avatar {
  width: 44px;
  height: 44px;
  flex: none;
  border-radius: 12px;
  display: grid;
  place-items: center;
  font-size: 18px;
  font-weight: 700;
  color: #fff;
  background: linear-gradient(135deg, #409eff, #7c3aed);
}

.avatar.guest {
  background: var(--el-fill-color-darker);
  color: var(--el-text-color-secondary);
}

.account-info {
  min-width: 0;
  flex: 1;
}

.account-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14.5px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.account-meta {
  margin-top: 3px;
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--el-color-success);
}

.account-actions {
  flex: none;
  display: flex;
  gap: 8px;
}

/* ---- 表单 ---- */
.form :deep(.el-form-item__label) {
  font-size: 13px;
  color: var(--el-text-color-regular);
  padding-bottom: 4px;
}

.field-hint {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

/* ---- 服务地址行 ---- */
/* ---- 本地模式 ---- */
.workspace-row {
  display: flex;
  gap: 8px;
  width: 100%;
}

.workspace-input {
  flex: 1;
  min-width: 0;
}

/* ---- 已记住的工具（权限记忆） ---- */
.remembered-block {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px dashed var(--el-border-color-lighter);
}

.remembered-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.remembered-title {
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.remembered-empty {
  margin-top: 6px;
}

.remembered-list {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 220px;
  overflow-y: auto;
}

.remembered-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 4px 8px;
  border-radius: 8px;
  background: var(--el-fill-color-lighter);
}

.remembered-name {
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
  color: var(--el-text-color-primary);
}

/* ---- 外观 ---- */
.theme-row {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.theme-tile {
  border: 1px solid var(--el-border-color-lighter);
  background: var(--el-bg-color);
  border-radius: 12px;
  padding: 10px;
  cursor: pointer;
  transition: border-color 0.15s, transform 0.15s;
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 168px;
}

.theme-tile:hover {
  border-color: var(--el-color-primary-light-5);
  transform: translateY(-1px);
}

.theme-tile.on {
  border-color: var(--el-color-primary);
  box-shadow: 0 0 0 1px var(--el-color-primary) inset;
}

.tile-preview {
  display: flex;
  height: 64px;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid var(--el-border-color-lighter);
}

.tile-preview.dark {
  background: #161616;
}

.tile-preview.light {
  background: #f5f7fa;
}

.tile-side {
  width: 34px;
  flex: none;
}

.tile-preview.dark .tile-side {
  background: #101010;
}

.tile-preview.light .tile-side {
  background: #e4e7ed;
}

.tile-main {
  flex: 1;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tile-main i {
  display: block;
  height: 8px;
  border-radius: 4px;
}

.tile-preview.dark .tile-main i {
  background: #262626;
}

.tile-preview.light .tile-main i {
  background: #dcdfe6;
}

.tile-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  color: var(--el-text-color-regular);
}

/* ---- 桌面 ---- */
.desk-list {
  display: flex;
  flex-direction: column;
}

.desk-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 11px 2px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.desk-row:last-child {
  border-bottom: none;
}

.desk-info {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.desk-name {
  font-size: 13.5px;
  color: var(--el-text-color-primary);
}

.desk-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.hotkey-control {
  width: 300px;
  flex: none;
}

.desk-control :deep(.el-input.is-invalid .el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--el-color-danger) inset;
}

.hotkey-status {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 5px;
  min-height: 18px;
  font-size: 12px;
}

.hotkey-note {
  color: var(--el-text-color-secondary);
}

.hotkey-bad {
  color: var(--el-color-danger);
}

/* ---- 关于 ---- */
.about-list {
  margin: 12px 0 0;
  padding-left: 18px;
  font-size: 12.5px;
  line-height: 1.9;
  color: var(--el-text-color-secondary);
}

.about-list strong {
  color: var(--el-text-color-primary);
  font-weight: 600;
}

.update-actions {
  flex: none;
  display: flex;
  gap: 8px;
}

.status-good {
  color: var(--el-color-success);
}

.status-warn {
  color: var(--el-color-warning);
}

.status-bad {
  color: var(--el-color-danger);
}

/* ---- 吸底操作条 ---- */
.action-bar {
  position: sticky;
  bottom: 0;
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 0 -4px -4px;
  padding: 10px 4px 4px;
  border-top: 1px solid var(--el-border-color-lighter);
  background: color-mix(in srgb, var(--el-bg-color) 88%, transparent);
  backdrop-filter: blur(8px);
}

.bar-left {
  display: flex;
  align-items: center;
  gap: 6px;
}

.bar-text {
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}

.bar-icon {
  color: var(--el-color-success);
}

.dirty-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--el-color-warning);
}

.bar-right {
  display: flex;
  gap: 8px;
}
</style>
