import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  ASR_CANCELED_ERROR,
  ASR_TIMEOUT_ERROR,
  ASR_UNCONFIGURED_ERROR,
  buildAsrForm,
  resolveAsrEndpoint,
  transcribeAudio,
  type AsrCallDeps
} from '../../electron/main/asr'

/** 语音转写(E)的请求组装与降级: 表单字段、鉴权头、未配置短路、取消/超时、错误折叠 */

const AUDIO = new Uint8Array([1, 2, 3, 4])

/** 捕获一次调用: URL / init / 表单条目 */
function fakeFetch(result: { ok: boolean; status?: number; body?: string }) {
  const calls: Array<{ url: string; init?: RequestInit }> = []
  const impl: typeof fetch = async (input, init) => {
    calls.push({ url: String(input), init })
    const body = result.body ?? ''
    return new Response(body, {
      status: result.status ?? (result.ok ? 200 : 500),
      headers: { 'content-type': 'application/json' }
    })
  }
  return { calls, impl }
}

/** 永不返回、仅响应 abort 的 fetch(用于超时/取消用例) */
function hangingFetch() {
  const impl: typeof fetch = (_input, init) =>
    new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal
      if (!signal) return
      if (signal.aborted) {
        reject(new DOMException('aborted', 'AbortError'))
        return
      }
      signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
    })
  return impl
}

test('asr: 端点解析兼容 base 与完整端点两种配置', () => {
  assert.equal(resolveAsrEndpoint(''), '')
  assert.equal(resolveAsrEndpoint('   '), '')
  assert.equal(resolveAsrEndpoint('https://ai.xzrobot.com/router/v1'), 'https://ai.xzrobot.com/router/v1/audio/transcriptions')
  assert.equal(resolveAsrEndpoint('https://ai.xzrobot.com/router/v1/'), 'https://ai.xzrobot.com/router/v1/audio/transcriptions')
  assert.equal(
    resolveAsrEndpoint('https://asr.example.com/v1/audio/transcriptions'),
    'https://asr.example.com/v1/audio/transcriptions'
  )
})

test('asr: 表单仅含 file(文件名+MIME), 不透传 model', () => {
  const form = buildAsrForm({ audio: AUDIO, filename: 'voice.webm', mime: 'audio/webm' })
  const file = form.get('file') as File
  assert.ok(file, 'file 字段必须存在')
  assert.equal(file.name, 'voice.webm')
  assert.equal(file.type, 'audio/webm')
  assert.equal(form.get('model'), null)

  const defaults = buildAsrForm({ audio: AUDIO })
  // 缺省文件名/MIME 有兜底
  assert.equal((defaults.get('file') as File).name, 'voice.webm')
  assert.equal((defaults.get('file') as File).type, 'audio/webm')
})

test('asr: 未配置地址时不发请求, 返回可读降级', async () => {
  const { calls, impl } = fakeFetch({ ok: true, body: '{"text":"x"}' })
  const res = await transcribeAudio({ asrUrl: '', audio: AUDIO, fetchImpl: impl })
  assert.deepEqual(res, { ok: false, error: ASR_UNCONFIGURED_ERROR })
  assert.equal(calls.length, 0)
  assert.equal(ASR_UNCONFIGURED_ERROR, '未配置语音服务')
})

test('asr: 正常路径 POST 到解析后的端点, 带 Bearer、multipart 表单与中止信号', async () => {
  const { calls, impl } = fakeFetch({ ok: true, body: '{"text":" 你好世界 "}' })
  const deps: AsrCallDeps = {
    asrUrl: 'https://ai.xzrobot.com/router/v1/',
    apiKey: 'router-key',
    audio: AUDIO,
    filename: 'voice-1.webm',
    mime: 'audio/webm;codecs=opus',
    fetchImpl: impl
  }
  const res = await transcribeAudio(deps)
  assert.deepEqual(res, { ok: true, text: '你好世界' })

  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, 'https://ai.xzrobot.com/router/v1/audio/transcriptions')
  assert.equal(calls[0].init?.method, 'POST')
  assert.ok(calls[0].init?.signal, '必须传 AbortSignal(超时兜底)')
  const headers = calls[0].init?.headers as Record<string, string>
  assert.equal(headers.authorization, 'Bearer router-key')
  const form = calls[0].init?.body as FormData
  assert.equal((form.get('file') as File).name, 'voice-1.webm')
  assert.equal((form.get('file') as File).type, 'audio/webm;codecs=opus')
})

test('asr: 无 key 时不带 Authorization(内网匿名端点可用)', async () => {
  const { calls, impl } = fakeFetch({ ok: true, body: '{"text":"ok"}' })
  await transcribeAudio({ asrUrl: 'https://asr.internal/v1', audio: AUDIO, fetchImpl: impl })
  const headers = (calls[0].init?.headers ?? {}) as Record<string, string>
  assert.equal(headers.authorization, undefined)
})

test('asr: HTTP 错误解析 OpenAI 兼容错误体, 保持可读', async () => {
  const { impl } = fakeFetch({
    ok: false,
    status: 401,
    body: '{"error":{"message":"invalid api key"}}'
  })
  const res = await transcribeAudio({ asrUrl: 'https://asr.example.com/v1', audio: AUDIO, fetchImpl: impl })
  assert.equal(res.ok, false)
  assert.match((res as { error: string }).error, /HTTP 401/)
  assert.match((res as { error: string }).error, /invalid api key/)
})

test('asr: 响应缺 text 与 fetch 抛错都折叠为 ok:false', async () => {
  const empty = await transcribeAudio({
    asrUrl: 'https://asr.example.com/v1',
    audio: AUDIO,
    fetchImpl: fakeFetch({ ok: true, body: '{}' }).impl
  })
  assert.deepEqual(empty, { ok: false, error: '语音服务未返回文本' })

  const failing: typeof fetch = async () => {
    throw new Error('connect ECONNREFUSED')
  }
  const res = await transcribeAudio({ asrUrl: 'https://asr.example.com/v1', audio: AUDIO, fetchImpl: failing })
  assert.equal(res.ok, false)
  assert.match((res as { error: string }).error, /ECONNREFUSED/)
})

test('asr: 超时(可注入)中止请求并返回专门文案', async () => {
  const res = await transcribeAudio({
    asrUrl: 'https://asr.example.com/v1',
    audio: AUDIO,
    fetchImpl: hangingFetch(),
    timeoutMs: 10
  })
  assert.deepEqual(res, { ok: false, error: ASR_TIMEOUT_ERROR })
  assert.equal(ASR_TIMEOUT_ERROR, '语音转写超时')
})

test('asr: 已取消的信号不再发请求', async () => {
  let called = 0
  const impl: typeof fetch = async () => {
    called += 1
    return new Response('{"text":"x"}')
  }
  const controller = new AbortController()
  controller.abort()
  const res = await transcribeAudio({
    asrUrl: 'https://asr.example.com/v1',
    audio: AUDIO,
    fetchImpl: impl,
    signal: controller.signal
  })
  assert.deepEqual(res, { ok: false, error: ASR_CANCELED_ERROR })
  assert.equal(called, 0)
})

test('asr: 请求途中取消返回已取消文案(外部 signal 优先于超时)', async () => {
  const controller = new AbortController()
  const pending = transcribeAudio({
    asrUrl: 'https://asr.example.com/v1',
    audio: AUDIO,
    fetchImpl: hangingFetch(),
    signal: controller.signal,
    timeoutMs: 5000
  })
  controller.abort()
  assert.deepEqual(await pending, { ok: false, error: ASR_CANCELED_ERROR })
})
