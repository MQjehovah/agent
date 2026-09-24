<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, del, put } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

interface LocalMcpEntry {
  name: string
  enabled?: boolean
  type?: string
  command?: string
  args?: string[]
  url?: string
  transport?: string
  cwd?: string
  env?: Record<string, string>
  description?: string
}

interface FormState {
  name: string
  enabled: boolean
  type: string
  command: string
  argsText: string
  url: string
  transport: string
  cwd: string
  envText: string
  description: string
}

const rows = ref<LocalMcpEntry[]>([])
const loading = ref(false)

const dialog = ref(false)
const editing = ref(false)
const saving = ref(false)
const form = ref<FormState>(emptyForm())

function emptyForm(): FormState {
  return {
    name: '', enabled: true, type: '', command: '', argsText: '',
    url: '', transport: '', cwd: '', envText: '', description: ''
  }
}

function endpoint(e: LocalMcpEntry): string {
  if (e.url) return e.url
  const parts = [e.command, ...(e.args ?? [])].filter(Boolean) as string[]
  return parts.join(' ') || '—'
}

async function load() {
  loading.value = true
  try {
    const d = await api<{ servers: LocalMcpEntry[] }>('/api/admin/local-mcp')
    rows.value = d.servers ?? []
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = false
  form.value = emptyForm()
  dialog.value = true
}

function openEdit(entry: LocalMcpEntry) {
  editing.value = true
  form.value = {
    name: entry.name,
    enabled: entry.enabled !== false,
    type: entry.type ?? '',
    command: entry.command ?? '',
    argsText: (entry.args ?? []).join('\n'),
    url: entry.url ?? '',
    transport: entry.transport ?? '',
    cwd: entry.cwd ?? '',
    envText: Object.entries(entry.env ?? {}).map(([k, v]) => `${k}=${v}`).join('\n'),
    description: entry.description ?? ''
  }
  dialog.value = true
}

function parseArgs(text: string): string[] {
  return text.split(/[\n,]/).map((s) => s.trim()).filter(Boolean)
}

function parseEnv(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const t = line.trim()
    if (!t || t.startsWith('#')) continue
    const idx = t.indexOf('=')
    if (idx <= 0) continue
    out[t.slice(0, idx).trim()] = t.slice(idx + 1).trim()
  }
  return out
}

async function save() {
  const name = form.value.name.trim()
  if (!name) {
    ElMessage.warning('名称必填')
    return
  }
  if (!form.value.command.trim() && !form.value.url.trim()) {
    ElMessage.warning('command 与 url 至少填一个')
    return
  }
  const body: Record<string, unknown> = {
    name,
    enabled: form.value.enabled,
    args: parseArgs(form.value.argsText),
    description: form.value.description
  }
  if (form.value.type.trim()) body.type = form.value.type.trim()
  if (form.value.command.trim()) body.command = form.value.command.trim()
  if (form.value.url.trim()) body.url = form.value.url.trim()
  if (form.value.transport.trim()) body.transport = form.value.transport.trim()
  if (form.value.cwd.trim()) body.cwd = form.value.cwd.trim()
  const env = parseEnv(form.value.envText)
  if (Object.keys(env).length) body.env = env

  saving.value = true
  try {
    await put(`/api/admin/local-mcp/${encodeURIComponent(name)}`, body)
    ElMessage.success('已保存（服务端已重载配置）')
    dialog.value = false
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    saving.value = false
  }
}

async function remove(entry: LocalMcpEntry) {
  try {
    await ElMessageBox.confirm(`确认删除本地安装「${entry.name}」? 删除后服务端将重载配置。`,
      '删除确认', { type: 'warning' })
  } catch { return }
  try {
    await del(`/api/admin/local-mcp/${encodeURIComponent(entry.name)}`)
    ElMessage.success('已删除（服务端已重载配置）')
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2>本地安装</h2>
        <div class="sub">本地安装仅管理员可用：写入全局 <code>config/mcp_servers.json</code>（服务端共享），保存/删除后由服务端重载，重启后仍生效</div>
      </div>
      <div class="actions">
        <el-button @click="load">刷新</el-button>
        <el-button type="primary" @click="openCreate">新建本地安装</el-button>
      </div>
    </div>

    <div v-loading="loading">
      <el-table v-if="rows.length" :data="rows" stripe>
        <el-table-column label="名称" prop="name" min-width="150" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.enabled === false ? 'info' : 'success'" effect="light">
              {{ row.enabled === false ? '已停用' : '已启用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="启动方式" min-width="260" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono">{{ endpoint(row) }}</span></template>
        </el-table-column>
        <el-table-column label="说明" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.description || '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" type="danger" plain @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-if="!loading && rows.length === 0"
                description="暂无本地 MCP 配置，点击「新建本地安装」添加">
        <el-button type="primary" @click="openCreate">新建本地安装</el-button>
      </el-empty>
    </div>

    <el-dialog v-model="dialog" :title="editing ? '编辑本地安装' : '新建本地安装'" width="640px">
      <el-form label-width="96px" label-position="right">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" :disabled="editing" placeholder="唯一标识，如 weather" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
        <el-form-item label="类型">
          <el-input v-model="form.type" placeholder="可选：stdio / sse / streamable-http" />
        </el-form-item>
        <el-form-item label="命令">
          <el-input v-model="form.command" placeholder="如 python（与 url 至少填一个）" />
        </el-form-item>
        <el-form-item label="参数">
          <el-input v-model="form.argsText" type="textarea" :rows="3"
                    placeholder="每行或逗号分隔，如&#10;mcp_server/src/default.py" />
        </el-form-item>
        <el-form-item label="URL">
          <el-input v-model="form.url" placeholder="远程 MCP 地址（与 command 至少填一个）" />
        </el-form-item>
        <el-form-item label="transport">
          <el-input v-model="form.transport" placeholder="可选，如 sse / streamable-http" />
        </el-form-item>
        <el-form-item label="工作目录">
          <el-input v-model="form.cwd" placeholder="可选，进程工作目录" />
        </el-form-item>
        <el-form-item label="环境变量">
          <el-input v-model="form.envText" type="textarea" :rows="3"
                    placeholder="每行 KEY=VALUE，如&#10;SMTP_PASSWORD=${SMTP_PASSWORD}" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存并重载</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.mono { font-family: Consolas, 'JetBrains Mono', monospace; font-size: 12.5px; }
code { background: var(--bg-hover); padding: 1px 5px; border-radius: 4px; }
</style>
