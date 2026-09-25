import { test } from 'node:test'
import assert from 'node:assert/strict'
import { isValidAccelerator, registerHotkeyWith, type HotkeyDeps } from '../../electron/main/hotkey'
import { isValidAccelerator as rendererIsValidAccelerator } from '../../src/utils/hotkey'

/** 主进程与渲染层两份校验的共用用例表 */
const ACCELERATOR_CASES = [
  'Alt+Space',
  'Ctrl+Shift+A',
  'ctrl+k',
  'CommandOrControl+Alt+P',
  'Super+D',
  'Ctrl+Alt+Shift+F12',
  '',
  '  ',
  'Space',
  'F2',
  'Ctrl',
  'Alt+Shift',
  'A+B',
  'Ctrl+Ctrl+A',
  'Ctrl+A+B',
  'Ctrl++A',
  'Alt+ Space',
  'Alt+',
  'Ctrl+Alt+Alt+Space',
  '命令+空格'
]

test('hotkey：主进程与渲染层校验规则一致(parity)', () => {
  for (const accel of ACCELERATOR_CASES) {
    assert.equal(isValidAccelerator(accel), rendererIsValidAccelerator(accel), accel)
  }
})

test('hotkey：常见组合合法', () => {
  for (const accel of ['Alt+Space', 'Ctrl+Shift+A', 'ctrl+k', 'CommandOrControl+Alt+P', 'Super+D', 'Ctrl+Alt+Shift+F12']) {
    assert.equal(isValidAccelerator(accel), true, accel)
  }
})

test('hotkey：缺少修饰键或缺少主键时非法', () => {
  for (const accel of ['', '  ', 'Space', 'F2', 'Ctrl', 'Alt+Shift', 'A+B']) {
    assert.equal(isValidAccelerator(accel), false, accel)
  }
})

test('hotkey：重复修饰键 / 多个主键 / 空片段时非法', () => {
  for (const accel of ['Ctrl+Ctrl+A', 'Ctrl+A+B', 'Ctrl++A', 'Alt+ Space']) {
    assert.equal(isValidAccelerator(accel), false, accel)
  }
})

test('hotkey：前后空白容忍', () => {
  assert.equal(isValidAccelerator('  Alt+Space  '), true)
  assert.equal(registerHotkeyWith(stubDeps().deps, '  Alt+Space  ', () => {}).ok, true)
})

function stubDeps(registerResult = true) {
  const calls: string[] = []
  const deps: HotkeyDeps = {
    unregisterAll: () => calls.push('unregisterAll'),
    register: (accelerator) => {
      calls.push(`register:${accelerator}`)
      return registerResult
    }
  }
  return { deps, calls }
}

test('hotkey：注册成功返回 ok,且先注销旧快捷键再注册', () => {
  const { deps, calls } = stubDeps(true)
  const res = registerHotkeyWith(deps, 'Alt+Space', () => {})
  assert.deepEqual(res, { ok: true })
  assert.deepEqual(calls, ['unregisterAll', 'register:Alt+Space'])
})

test('hotkey：被占用时返回错误信息,不抛异常', () => {
  const { deps, calls } = stubDeps(false)
  const res = registerHotkeyWith(deps, 'Alt+Space', () => {})
  assert.equal(res.ok, false)
  assert.match(res.error ?? '', /被占用/)
  assert.deepEqual(calls, ['unregisterAll', 'register:Alt+Space'])
})

test('hotkey：格式非法时不调用 register,也不动当前快捷键(不 unregister)', () => {
  const { deps, calls } = stubDeps(true)
  const res = registerHotkeyWith(deps, 'Alt+', () => {})
  assert.equal(res.ok, false)
  assert.match(res.error ?? '', /格式非法/)
  assert.deepEqual(calls, [])
})

test('hotkey：空快捷键视为未设置(注销后直接返回,不调用 register)', () => {
  const { deps, calls } = stubDeps(true)
  const res = registerHotkeyWith(deps, '  ', () => {})
  assert.equal(res.ok, false)
  assert.match(res.error ?? '', /未设置快捷键/)
  assert.deepEqual(calls, ['unregisterAll'])
  // 未设置不是 disabled 业务态,disabled 由 quick.ts 在空配置时补充
  assert.equal(res.disabled, undefined)
})

test('hotkey：register 抛异常时降级为错误结果', () => {
  const deps: HotkeyDeps = {
    unregisterAll: () => {},
    register: () => {
      throw new Error('native failure')
    }
  }
  const res = registerHotkeyWith(deps, 'Ctrl+K', () => {})
  assert.equal(res.ok, false)
  assert.match(res.error ?? '', /native failure/)
})

test('hotkey：注册成功后回调指向触发函数', () => {
  let fired = 0
  const deps: HotkeyDeps = {
    unregisterAll: () => {},
    register: (_accelerator, callback) => {
      callback()
      return true
    }
  }
  registerHotkeyWith(deps, 'Ctrl+K', () => {
    fired += 1
  })
  assert.equal(fired, 1)
})
