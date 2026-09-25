import { test } from 'node:test'
import assert from 'node:assert/strict'
import { streamRequest, ApiError } from '../../src/api/client'
import { shouldNotifyStreamFinish } from '../../src/utils/stream'

/**
 * client.ts 直接使用渲染层全局 window.desktop;此测试在 Node 下注入最小桥接。
 * 该文件被 tsconfig.node 收录,故显式声明 window 形状(DOM 类型不参与 node 检查)。
 */
interface MockUpstreamEvent {
  streamId: string
  type: 'status' | 'chunk' | 'end' | 'error'
  status?: number
  text?: string
  message?: string
  aborted?: boolean
}

declare global {
  var window: {
    desktop: {
      invoke<T = unknown>(channel: string, payload?: unknown): Promise<T>
      onUpstreamEvent(callback: (event: MockUpstreamEvent) => void): () => void
    }
  }
}

function createMockDesktop(options: { deferStart?: boolean } = {}) {
  const listeners: Array<(event: MockUpstreamEvent) => void> = []
  const invokes: string[] = []
  let resolveStart: ((value: { streamId: string }) => void) | null = null
  const desktop = {
    invoke: async <T,>(channel: string): Promise<T> => {
      invokes.push(channel)
      if (channel === 'upstream:stream:start') {
        if (options.deferStart) {
          return new Promise<{ streamId: string }>((resolve) => {
            resolveStart = resolve
          }) as Promise<T>
        }
        return { streamId: 's-1' } as T
      }
      return undefined as T
    },
    onUpstreamEvent: (callback: (event: MockUpstreamEvent) => void): (() => void) => {
      listeners.push(callback)
      return () => {
        const index = listeners.indexOf(callback)
        if (index >= 0) listeners.splice(index, 1)
      }
    }
  }
  const emit = (event: MockUpstreamEvent): void => {
    for (const listener of [...listeners]) listener(event)
  }
  const releaseStart = (): void => {
    resolveStart?.({ streamId: 's-1' })
    resolveStart = null
  }
  return { desktop, emit, invokes, listenerCount: () => listeners.length, releaseStart }
}

function installWindow(desktop: { invoke: unknown; onUpstreamEvent: unknown }): void {
  ;(globalThis as unknown as { window: unknown }).window = { desktop }
}

const tick = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0))

test('stream-request：end(aborted:true) 透出 aborted,供调用方跳过完成通知', async () => {
  const mock = createMockDesktop()
  installWindow(mock.desktop)
  const frames: unknown[] = []

  const pending = streamRequest<{ v: string }>('agent', '/api/chat/stream', {}, (frame) => frames.push(frame))
  await tick()
  mock.emit({ streamId: 's-1', type: 'status', status: 200 })
  mock.emit({ streamId: 's-1', type: 'chunk', text: 'data: {"v":"x"}\n\n' })
  mock.emit({ streamId: 's-1', type: 'end', aborted: true })

  assert.deepEqual(await pending, { aborted: true })
  assert.equal(frames.length, 1)
  // 收尾后摘除监听
  assert.equal(mock.listenerCount(), 0)
})

test('stream-request：正常 end 返回 aborted:false,并处理未以空行结束的尾帧', async () => {
  const mock = createMockDesktop()
  installWindow(mock.desktop)
  const frames: Array<{ v: string }> = []

  const pending = streamRequest<{ v: string }>('agent', '/api/chat/stream', {}, (frame) => frames.push(frame))
  await tick()
  mock.emit({ streamId: 's-1', type: 'chunk', text: 'data: {"v":"tail"}' })
  mock.emit({ streamId: 's-1', type: 'end' })

  assert.deepEqual(await pending, { aborted: false })
  assert.deepEqual(frames, [{ v: 'tail' }])
})

test('stream-request：error 事件以 ApiError 拒绝', async () => {
  const mock = createMockDesktop()
  installWindow(mock.desktop)

  const pending = streamRequest('agent', '/api/chat/stream', {}, () => {})
  await tick()
  mock.emit({ streamId: 's-1', type: 'error', status: 500, message: '服务异常' })

  await assert.rejects(pending, (err: Error) => {
    assert.ok(err instanceof ApiError)
    assert.match(err.message, /服务异常/)
    return true
  })
})

test('stream-request：HTTP 错误状态以 ApiError 拒绝', async () => {
  const mock = createMockDesktop()
  installWindow(mock.desktop)

  const pending = streamRequest('agent', '/api/chat/stream', {}, () => {})
  await tick()
  mock.emit({ streamId: 's-1', type: 'status', status: 401 })

  await assert.rejects(pending, (err: Error) => {
    assert.ok(err instanceof ApiError)
    assert.match(err.message, /HTTP 401/)
    return true
  })
})

test('stream-request：其它 streamId 的事件被忽略', async () => {
  const mock = createMockDesktop()
  installWindow(mock.desktop)
  const frames: unknown[] = []

  const pending = streamRequest('agent', '/api/chat/stream', {}, (frame) => frames.push(frame))
  await tick()
  mock.emit({ streamId: 'other', type: 'chunk', text: 'data: {"v":"no"}\n\n' })
  mock.emit({ streamId: 's-1', type: 'end' })

  assert.deepEqual(await pending, { aborted: false })
  assert.equal(frames.length, 0)
})

test('stream-request：signal 中止时通知主进程 abort', async () => {
  const mock = createMockDesktop()
  installWindow(mock.desktop)
  const controller = new AbortController()

  const pending = streamRequest('agent', '/api/chat/stream', {}, () => {}, controller.signal)
  await tick()
  controller.abort()
  await tick()

  assert.ok(mock.invokes.includes('upstream:stream:abort'))
  mock.emit({ streamId: 's-1', type: 'end', aborted: true })
  assert.deepEqual(await pending, { aborted: true })
})

test('stream-request：start 期间已中止的 signal 也会补发 abort', async () => {
  const mock = createMockDesktop({ deferStart: true })
  installWindow(mock.desktop)
  const controller = new AbortController()

  const pending = streamRequest('agent', '/api/chat/stream', {}, () => {}, controller.signal)
  await tick()
  // start 尚未返回时用户已点停止:addEventListener 不会再触发,必须补发 abort
  controller.abort()
  mock.releaseStart()
  await tick()

  assert.ok(mock.invokes.includes('upstream:stream:abort'))
  mock.emit({ streamId: 's-1', type: 'end', aborted: true })
  assert.deepEqual(await pending, { aborted: true })
})

test('stream-finish：中止 / 出错 / 空内容不发完成通知', () => {
  assert.equal(shouldNotifyStreamFinish({ aborted: true, error: '', content: '已有内容' }), false)
  assert.equal(shouldNotifyStreamFinish({ aborted: false, error: '网络错误', content: '已有内容' }), false)
  assert.equal(shouldNotifyStreamFinish({ aborted: false, error: '', content: '   \n ' }), false)
})

test('stream-finish：正常完成且有内容才发完成通知', () => {
  assert.equal(shouldNotifyStreamFinish({ aborted: false, error: '', content: '回答完成' }), true)
})
