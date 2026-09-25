/**
 * 语音转写（E 阶段）主进程纯逻辑：OpenAI 兼容 `/audio/transcriptions` 请求组装与调用。
 *
 * 不 import electron：地址与 token 由 IPC handler 注入（token 走 identity.ensureRouterKey），
 * fetch 可注入便于单测锁定表单字段、鉴权头、取消/超时与降级行为。
 */

export const ASR_UNCONFIGURED_ERROR = '未配置语音服务'
/** 单次转写默认超时：挂起请求最多占用 30s（渲染层取消经 IPC 透传 signal 可提前中止） */
export const ASR_TIMEOUT_MS = 30_000
export const ASR_TIMEOUT_ERROR = '语音转写超时'
export const ASR_CANCELED_ERROR = '语音转写已取消'

export interface AsrCallDeps {
  /** 企业配置的 ASR 地址（OpenAI 兼容 base，如 https://ai.xzrobot.com/router/v1） */
  asrUrl: string
  /** Bearer token（ensureRouterKey 结果）；空串时仍可请求未鉴权的内网端点 */
  apiKey?: string
  /** 音频字节 */
  audio: Uint8Array | ArrayBuffer
  /** 上传文件名，缺省 voice.webm */
  filename?: string
  /** 音频 MIME，缺省 audio/webm */
  mime?: string
  /** 外部取消信号（渲染层「取消转写」经 IPC 透传）；已中止时不再发请求 */
  signal?: AbortSignal
  /** 超时毫秒；默认 ASR_TIMEOUT_MS，单测可注入 */
  timeoutMs?: number
  /** 可注入 fetch（单测用） */
  fetchImpl?: typeof fetch
}

export type AsrResult = { ok: true; text: string } | { ok: false; error: string }

/**
 * 组装转写端点：已指向 `/audio/transcriptions` 时原样使用，
 * 否则去掉尾斜杠后追加 `/audio/transcriptions`（兼容配 base 与配完整端点两种写法）。
 */
export function resolveAsrEndpoint(asrUrl: string): string {
  const base = String(asrUrl ?? '').trim().replace(/\/+$/, '')
  if (!base) return ''
  return /\/audio\/transcriptions$/i.test(base) ? base : `${base}/audio/transcriptions`
}

/** 组装 multipart 表单：仅 file（含文件名与 MIME）；model 由服务端默认，不在此透传 */
export function buildAsrForm(input: {
  audio: Uint8Array | ArrayBuffer
  filename?: string
  mime?: string
}): FormData {
  const form = new FormData()
  const bytes = input.audio instanceof ArrayBuffer ? new Uint8Array(input.audio) : input.audio
  const mime = String(input.mime ?? '').trim() || 'audio/webm'
  const filename = String(input.filename ?? '').trim() || 'voice.webm'
  form.append('file', new Blob([bytes], { type: mime }), filename)
  return form
}

/** 从 OpenAI 兼容错误体里取可读原因（`{error:{message}}` / `{error}` / `{detail}`） */
function parseAsrErrorBody(text: string): string {
  try {
    const parsed = JSON.parse(text) as { error?: unknown; detail?: unknown }
    const err = parsed?.error
    if (typeof err === 'string' && err.trim()) return err.trim()
    if (typeof err === 'object' && err !== null) {
      const message = (err as { message?: unknown }).message
      if (typeof message === 'string' && message.trim()) return message.trim()
    }
    if (typeof parsed?.detail === 'string' && parsed.detail.trim()) return parsed.detail.trim()
  } catch {
    // 非 JSON 响应回退原文
  }
  return text.trim().slice(0, 200)
}

/**
 * 调 ASR 转写；一切失败折叠为 `{ ok:false, error }`，不抛错（IPC 边界友好）。
 * 未配置地址或已取消时不发任何请求；默认 30s 超时，超时/取消给专门文案。
 */
export async function transcribeAudio(deps: AsrCallDeps): Promise<AsrResult> {
  const endpoint = resolveAsrEndpoint(deps.asrUrl)
  if (!endpoint) return { ok: false, error: ASR_UNCONFIGURED_ERROR }
  if (deps.signal?.aborted) return { ok: false, error: ASR_CANCELED_ERROR }
  const timeoutMs =
    Number.isFinite(deps.timeoutMs) && (deps.timeoutMs as number) > 0 ? (deps.timeoutMs as number) : ASR_TIMEOUT_MS
  const timeoutSignal = AbortSignal.timeout(timeoutMs)
  const signal = deps.signal ? AbortSignal.any([deps.signal, timeoutSignal]) : timeoutSignal
  const form = buildAsrForm({ audio: deps.audio, filename: deps.filename, mime: deps.mime })
  const headers: Record<string, string> = {}
  const key = String(deps.apiKey ?? '').trim()
  if (key) headers.authorization = `Bearer ${key}`
  const fetchImpl = deps.fetchImpl ?? fetch
  try {
    const res = await fetchImpl(endpoint, { method: 'POST', headers, body: form, signal })
    if (!res.ok) {
      const body = await res.text().catch(() => '')
      const reason = parseAsrErrorBody(body) || `HTTP ${res.status}`
      return { ok: false, error: `语音转写失败（HTTP ${res.status}）：${reason}` }
    }
    const data = (await res.json()) as { text?: unknown }
    const text = typeof data?.text === 'string' ? data.text.trim() : ''
    if (!text) return { ok: false, error: '语音服务未返回文本' }
    return { ok: true, text }
  } catch (err) {
    if (deps.signal?.aborted) return { ok: false, error: ASR_CANCELED_ERROR }
    if (timeoutSignal.aborted) return { ok: false, error: ASR_TIMEOUT_ERROR }
    return { ok: false, error: `语音转写请求失败：${(err as Error).message}` }
  }
}
