<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

interface SecretRow { id: string; key_name: string; scope: string; has_value: boolean }
interface StatusResp { required: string[]; filled: string[]; missing: string[]; complete: boolean }

const props = defineProps<{
  modelValue: boolean
  capabilityId: string
  capabilityName: string
  keys: string[]
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()

const visible = computed({ get: () => props.modelValue, set: (v) => emit('update:modelValue', v) })
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const keys = ref<string[]>([])
const rows = ref<SecretRow[]>([])
const status = ref<StatusResp | null>(null)
const inputs = ref<Record<string, string>>({})

function invokeErr(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err)
  return raw.replace(/^Error invoking remote method '[^']*':\s*(Error:\s*)?/, '')
}

function rowOf(key: string): SecretRow | undefined {
  return rows.value.find((r) => r.key_name === key && r.scope === props.capabilityId)
}
/** 本能力已设置 / 仅全局已设置 / 未设置 */
function keyState(key: string): 'cap' | 'global' | 'missing' {
  if (rowOf(key)?.has_value) return 'cap'
  if (status.value?.filled?.includes(key)) return 'global'
  return 'missing'
}

let loadSeq = 0

async function load(): Promise<void> {
  const seq = ++loadSeq
  loading.value = true
  error.value = ''
  inputs.value = {}
  rows.value = []
  status.value = null
  keys.value = [...props.keys]
  try {
    const [list, st] = await Promise.all([
      window.desktop.invoke<SecretRow[]>('localagent:market:secrets:list', {
        scope: props.capabilityId
      }),
      window.desktop.invoke<StatusResp>('localagent:market:secrets:status', {
        capabilityId: props.capabilityId
      })
    ])
    if (seq !== loadSeq) return
    rows.value = Array.isArray(list) ? list : []
    status.value = st
    if (st?.required?.length) keys.value = st.required
  } catch (err) {
    if (seq !== loadSeq) return
    error.value = invokeErr(err)
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

async function save(): Promise<void> {
  const secrets: Record<string, string> = {}
  for (const [k, v] of Object.entries(inputs.value)) if (v && v.trim()) secrets[k] = v
  if (!Object.keys(secrets).length) {
    ElMessage.warning('请先填写至少一项凭据')
    return
  }
  saving.value = true
  try {
    await window.desktop.invoke('localagent:market:secrets:upsert', {
      secrets,
      scope: props.capabilityId
    })
    ElMessage.success('凭据已保存(仅本人可用, 加密存储)')
    await load()
  } catch (err) {
    ElMessage.error(`保存失败:${invokeErr(err)}`)
  } finally {
    saving.value = false
  }
}

async function clearKey(key: string): Promise<void> {
  if (loading.value || saving.value) return
  const row = rowOf(key)
  if (!row) return
  try {
    await window.desktop.invoke('localagent:market:secrets:delete', { id: row.id })
    ElMessage.success(`已清除「${key}」的本能力凭据`)
    await load()
  } catch (err) {
    ElMessage.error(`清除失败:${invokeErr(err)}`)
  }
}

watch(() => props.modelValue, (v) => {
  if (v) void load()
  else inputs.value = {}
})
</script>

<template>
  <el-dialog v-model="visible" :title="`配置凭据 · ${capabilityName}`" width="520px">
    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error"
              style="margin-bottom: 10px" />
    <div v-loading="loading">
      <p class="cred-hint">
        该能力以你的身份执行，以下凭据仅本人可用、加密存储且不回显；填写后保存到「本能力」。
        留空表示不修改。
      </p>
      <div v-for="key in keys" :key="key" class="cred-row">
        <div class="cred-key">
          <span class="cred-name">{{ key }}</span>
          <el-tag v-if="keyState(key) === 'cap'" size="small" type="success">已设置·本能力</el-tag>
          <el-tag v-else-if="keyState(key) === 'global'" size="small" type="info">已设置·全局</el-tag>
          <el-tag v-else size="small" type="warning">未设置</el-tag>
        </div>
        <el-input v-model="inputs[key]" type="password" show-password
                  :placeholder="keyState(key) === 'missing' ? '请输入' : '已设置, 留空不修改'" />
        <el-button v-if="rowOf(key)" size="small" text type="danger"
                   :disabled="loading || saving" @click="clearKey(key)">
          清除
        </el-button>
      </div>
      <el-empty v-if="!keys.length && !loading" description="该能力未声明用户凭据" />
    </div>
    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
      <el-button type="primary" :loading="saving" :disabled="!keys.length || loading" @click="save">
        保存
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.cred-hint { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.7; margin: 0 0 10px; }
.cred-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.cred-key { width: 190px; flex: none; display: flex; align-items: center; gap: 6px; }
.cred-name { font-family: ui-monospace, monospace; font-size: 12px; word-break: break-all; }
</style>
