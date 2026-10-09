import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  createOidcRefresher,
  type OidcRefreshResponse,
  type OidcTokensLike
} from '../../electron/main/oidc-refresh'

interface FakeState {
  tokens: OidcTokensLike | null
  clock: number
  calls: string[]
  refreshImpl: (refreshToken: string) => Promise<OidcRefreshResponse>
  applied: OidcTokensLike[]
  afterRefreshCalls: number
  /** 模拟刷新期间重新登录:applyTokens 返回 false(代际已变) */
  dropApply: boolean
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (err: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function tokens(expiresAt: number, overrides: Partial<OidcTokensLike> = {}): OidcTokensLike {
  return { idToken: 'id-1', accessToken: 'at-1', refreshToken: 'rt-1', expiresAt, ...overrides }
}

function setup(initial: OidcTokensLike | null, expiresIn = 3600) {
  const state: FakeState = {
    tokens: initial,
    clock: 1_000_000,
    calls: [],
    refreshImpl: async () => ({ access_token: 'at-new', refresh_token: 'rt-new', expires_in: expiresIn }),
    applied: [],
    afterRefreshCalls: 0,
    dropApply: false
  }

  const refresher = createOidcRefresher({
    getTokens: () => state.tokens,
    applyTokens: (next, expected) => {
      if (state.dropApply || state.tokens !== expected) return false
      state.tokens = next
      state.applied.push(next)
      return true
    },
    refresh: (refreshToken) => {
      state.calls.push(refreshToken)
      return state.refreshImpl(refreshToken)
    },
    afterRefresh: () => {
      state.afterRefreshCalls++
    },
    now: () => state.clock
  })

  return { refresher, state }
}

test('ensureFresh: 未临近过期(余量 >60s)时不发请求', async () => {
  const { refresher, state } = setup(tokens(1_000_000 + 10 * 60_000))
  await refresher.ensureFresh()
  assert.equal(state.calls.length, 0)
})

test('ensureFresh: 无身份(tokens 为 null)时静默返回', async () => {
  const { refresher, state } = setup(null)
  await refresher.ensureFresh()
  assert.equal(state.calls.length, 0)
})

test('ensureFresh: 无 refresh_token 时抛错', async () => {
  const { refresher } = setup(tokens(1_000_000, { refreshToken: '' }))
  await assert.rejects(refresher.ensureFresh(), /无 refresh_token/)
})

test('ensureFresh: 过期时刷新并按轮换语义写回(保留旧 idToken,新 expiresAt=now+expires_in)', async () => {
  const { refresher, state } = setup(tokens(1_000_000))
  await refresher.ensureFresh()
  assert.deepEqual(state.calls, ['rt-1'])
  assert.equal(state.applied.length, 1)
  assert.deepEqual(state.applied[0], {
    idToken: 'id-1',
    accessToken: 'at-new',
    refreshToken: 'rt-new',
    expiresAt: state.clock + 3600 * 1000
  })
  assert.equal(state.afterRefreshCalls, 1)
})

test('ensureFresh: 响应缺 refresh_token 时沿用旧值', async () => {
  const { refresher, state } = setup(tokens(1_000_000))
  state.refreshImpl = async () => ({ access_token: 'at-new', expires_in: 60 })
  await refresher.ensureFresh()
  assert.equal(state.applied[0].refreshToken, 'rt-1')
})

test('ensureFresh: 并发调用合并为一次刷新(单飞)', async () => {
  const { refresher, state } = setup(tokens(1_000_000))
  const gate = deferred<OidcRefreshResponse>()
  state.refreshImpl = () => gate.promise
  const p1 = refresher.ensureFresh()
  const p2 = refresher.ensureFresh()
  const p3 = refresher.ensureFresh()
  assert.equal(state.calls.length, 1)
  gate.resolve({ access_token: 'at-new', refresh_token: 'rt-new', expires_in: 3600 })
  await Promise.all([p1, p2, p3])
  assert.equal(state.applied.length, 1)
  assert.equal(state.afterRefreshCalls, 1)
})

test('ensureFresh: 单飞期间失败时所有等待者收到同一错误', async () => {
  const { refresher, state } = setup(tokens(1_000_000))
  const gate = deferred<OidcRefreshResponse>()
  state.refreshImpl = () => gate.promise
  const p1 = refresher.ensureFresh()
  const p2 = refresher.ensureFresh()
  const boom = new Error('刷新失败(HTTP 400)')
  gate.reject(boom)
  await assert.rejects(p1, /刷新失败\(HTTP 400\)/)
  await assert.rejects(p2, /刷新失败\(HTTP 400\)/)
  assert.equal(state.calls.length, 1)
})

test('ensureFresh: 失败后进入 60s 冷却(直接抛同错,不发新请求),冷却过后可重试', async () => {
  const { refresher, state } = setup(tokens(1_000_000))
  const boom = new Error('刷新失败(HTTP 400)')
  state.refreshImpl = async () => {
    throw boom
  }
  await assert.rejects(refresher.ensureFresh(), boom)

  state.clock += 30_000
  await assert.rejects(refresher.ensureFresh(), boom)
  assert.equal(state.calls.length, 1)

  state.clock += 31_000
  state.refreshImpl = async () => ({ access_token: 'at-new', expires_in: 3600 })
  await refresher.ensureFresh()
  assert.equal(state.calls.length, 2)
  assert.equal(state.applied.length, 1)
})

test('ensureFresh: resetFailure 立即清除冷却', async () => {
  const { refresher, state } = setup(tokens(1_000_000))
  state.refreshImpl = async () => {
    throw new Error('刷新失败(HTTP 400)')
  }
  await assert.rejects(refresher.ensureFresh())
  refresher.resetFailure()
  state.refreshImpl = async () => ({ access_token: 'at-new', expires_in: 3600 })
  await refresher.ensureFresh()
  assert.equal(state.calls.length, 2)
})

test('ensureFresh: 刷新成功清除失败标记(后续立即刷新不再触发冷却)', async () => {
  const { refresher, state } = setup(tokens(1_000_000))
  state.refreshImpl = async () => {
    throw new Error('刷新失败(HTTP 400)')
  }
  await assert.rejects(refresher.ensureFresh())
  state.clock += 61_000
  state.refreshImpl = async () => ({ access_token: 'at-new', expires_in: 1 })
  await refresher.ensureFresh()
  // 过期后立刻再刷:无冷却,直接发第二次请求
  state.clock += 61_000
  await refresher.ensureFresh()
  assert.equal(state.calls.length, 3)
})

test('ensureFresh: 刷新期间重新登录(代际变化)时丢弃结果,不触发副作用,也不视为失败', async () => {
  const { refresher, state } = setup(tokens(1_000_000))
  state.dropApply = true
  await refresher.ensureFresh()
  assert.equal(state.calls.length, 1)
  assert.equal(state.applied.length, 0)
  assert.equal(state.afterRefreshCalls, 0)
  // 未被标记失败:立即再调(仍过期)会重新发起刷新,而不是抛冷却错误
  state.dropApply = false
  await refresher.ensureFresh()
  assert.equal(state.calls.length, 2)
})
