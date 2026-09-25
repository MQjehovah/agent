import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mergeConnectorEnv } from '../../electron/main/kernel/mcp-env'

test('mergeConnectorEnv：保留 auto 变量原值（覆盖与删除均忽略）', () => {
  const current = {
    MARKET_URL: '${MARKET_URL}',
    MARKET_TOKEN: '${MARKET_TOKEN}',
    DINGTALK_APP_SECRET: '${DINGTALK_APP_SECRET}'
  }
  const out = mergeConnectorEnv(
    current,
    { MARKET_URL: 'http://evil.local', MARKET_TOKEN: '', DINGTALK_APP_SECRET: 'my-secret' },
    ['MARKET_URL', 'MARKET_TOKEN']
  )
  assert.equal(out.MARKET_URL, '${MARKET_URL}')
  assert.equal(out.MARKET_TOKEN, '${MARKET_TOKEN}')
  assert.equal(out.DINGTALK_APP_SECRET, 'my-secret')
})

test('mergeConnectorEnv：空字符串删除已配置键，非空覆写（未提交的键保留）', () => {
  const current = { A: 'old', B: '${B}', C: 'keep' }
  const out = mergeConnectorEnv(current, { A: 'new', B: '  ' }, [])
  assert.equal(out.A, 'new')
  assert.ok(!('B' in out))
  assert.equal(out.C, 'keep')
})

test('mergeConnectorEnv：非法键名整批拒绝（不做半写）', () => {
  assert.throws(() => mergeConnectorEnv({ A: '1' }, { 'BAD-KEY': 'x' }, []), /环境变量名非法/)
  assert.throws(() => mergeConnectorEnv({}, { '1A': 'x' }, []), /环境变量名非法/)
  assert.throws(() => mergeConnectorEnv({}, { '含中文': 'x' }, []), /环境变量名非法/)
})

test('mergeConnectorEnv：当前值本身是托管占位符时也保留（未显式传 autoVars）', () => {
  const out = mergeConnectorEnv(
    { MARKET_TOKEN: '${MARKET_TOKEN}' },
    { MARKET_TOKEN: 'stolen' },
    []
  )
  assert.equal(out.MARKET_TOKEN, '${MARKET_TOKEN}')
})
